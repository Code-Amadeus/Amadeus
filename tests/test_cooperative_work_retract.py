"""Work withdrawal shares task cancellation and cannot become new execution."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.auip_b2 import AuipB2Coordinator
from server.auip_control_decision import AuipControlDecisionResolver
from server.event_bus import bus
from server.protocol import Method
from server.cooperative_provider_loop import _normalize_work_action, LoopConflict
from test_auip_b2 import _runtime
from test_auip_control_decision import _Catalog
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_provider_loop import loop_host as loop_host


@pytest.mark.parametrize("state", ["active", "terminal", "unknown_target"])
@pytest.mark.parametrize("app_action", ["none", "read", "step"])
async def test_work_retract_preserves_artifact_and_independent_application(pending_host, state, app_action):
    context=pending_host
    runtime, registered=_runtime(conversation_id=context.session_id)
    sid=registered['app_session_id']
    app_query=AsyncMock(return_value=json.dumps({'work_relation':'independent',
        **({'action':'step','instruction':'这步你来。'} if app_action=='step' else
            {'action':'none','read':['state']})}))
    text='博客那个先别做了。' if state=='unknown_target' else '便签先别做了。'
    if app_action=='step':
        text='这步你来，'+text
    elif app_action=='read':
        text='现在局面怎么样？'+text
    target=''
    role_queries=[]
    reference_queries=[]
    async def query(messages, **_kwargs):
        try:
            frame=json.loads(messages[-1]['content'])
        except json.JSONDecodeError:
            reference_queries.append(messages)
            assert text in messages[-1]['content']
            return json.dumps({'references':[] if state=='unknown_target' else [target]})
        role_queries.append(frame)
        if frame['source_kind']!='user':
            return '実行状態を確認したわ。'
        if frame['current']['text']=='做个便签页吧。':
            action={'op':'work','intent':'execute'}
        else:
            action={'op':'work','intent':'retract','target':target}
            if app_action!='none':
                action['source']='博客那个先别做了。' if state=='unknown_target' else '便签先别做了。'
        return json.dumps({'action':action,'say':'便箋の作業を止めるわ。'})
    context.manager.query=query
    context.host.adapter.release.clear()
    app_calls=[]
    async def choose(**kwargs):
        candidate=next(c for c in kwargs['candidates'].values()
            if c.action_type=='game.place' and c.payload=={'x':1,'y':1})
        return {'candidate_id':candidate.candidate_id,'instruction_relation':'follows',
            'choice_reason':'the square is available','speech':'右下に置いたわ。'}
    async def app_receipt(_method,payload):
        if payload.get('app_session_id')!=sid:
            return
        action=payload['action']
        app_calls.append(action)
        snapshot=runtime.get(sid)['state']
        snapshot['board']['rows']=['B.','.B']
        resolved=runtime.resolve_action(app_session_id=sid,bridge_token=registered['bridge_token'],
            action_id=action['action_id'],accepted=True,resulting_revision=2,state=snapshot)
        await bus.emit(Method.AUIP_UPDATED,resolved)
    bus.on(Method.AUIP_ACTION_REQUESTED,app_receipt)
    try:
        await context.handler.send_text('做个便签页吧。',session_id=context.session_id,turn_id='create-memo')
        await asyncio.wait_for(context.handler._stream_task,3)
        await asyncio.wait_for(context.host.adapter.started.wait(),3)
        ingress=context.manager.ingresses[context.session_id]
        original=ingress.receipts['create-memo']
        artifact=Path(context.host.adapter.requests[0]['request'].cwd)/'index.html'
        artifact.write_bytes(b'preserve the accepted memo')
        # Reproduce the real-model error: an absent named target was rewritten
        # to the current Work's valid identity. Membership is not semantic fit.
        target='work_item:'+original['work_item_id']
        if state=='terminal':
            context.host.adapter.release.set()
            await context.finish()
        context.host.runtime.cancel=AsyncMock(wraps=context.host.runtime.cancel)
        if app_action!='none':
            decider=AuipControlDecisionResolver(query=app_query,app_runtime=runtime,launch_catalog=_Catalog())
            b2=AuipB2Coordinator(runtime=runtime,control_decider=decider,role_chooser=choose,
                stage_decision=lambda *_:pytest.fail('unexpected legacy fallback'))
            context.manager.configure_auip(decider,AsyncMock(side_effect=AssertionError('do not duplicate the app lane')),
                step_request=b2.execute_user_decision)
        context.publications.clear()
        context.spoken.clear()
        await context.handler.send_text(text,session_id=context.session_id,turn_id='withdraw-memo')
        await asyncio.wait_for(context.handler._stream_task,3)
        result=ingress.receipts['withdraw-memo']
        work=result['work'] if app_action!='none' else result
        assert work['state']=={'active':'stopped','terminal':'not_active','unknown_target':'rejected'}[state]
        assert work['action']=='interrupt' and work['question']==text
        if state=='terminal':
            # The same received facts must reach both ordinary and combined
            # expression; a promised stop is not a cancellation receipt.
            expressed=[frame['current'] for frame in role_queries
                if frame['source_kind']=='host_receipt'
                and (frame['current'].get('state')=='not_active'
                    or frame['current'].get('work',{}).get('state')=='not_active')]
            assert len(expressed)==1
            facts=expressed[0].get('work',expressed[0])
            assert facts['question']==text and facts['action']=='interrupt'
            assert facts['status']=='succeeded'
        assert context.host.runtime.cancel.await_count==int(state=='active')
        if state=='active':
            context.host.runtime.cancel.assert_awaited_once_with(original['run_id'])
        assert context.host.adapter.calls==1
        assert len(reference_queries)==1
        assert len(context.host.work.list_work_items())==1
        assert len(context.host.work.list_attempts(original['work_item_id']))==1
        assert context.host.work.list_provider_inputs(original['work_item_id'])==[]
        assert artifact.read_bytes()==b'preserve the accepted memo'
        assert runtime.get(sid)['status']=='active'
        assert runtime.get(sid)['revision']==(2 if app_action=='step' else 1)
        assert len(app_calls)==int(app_action=='step')
        assert len(context.publications)==len(context.spoken)==1
        assert context.publications[0]['cause']=='withdraw-memo'
        row=context.host.control_store.find_admission('chat:'+context.session_id,'withdraw-memo')
        assert json.loads(row['plan_json'])['effects']==[]
        assert (await context.handler.send_text(text,session_id=context.session_id,turn_id='withdraw-memo'))['status']=='replayed'
        assert context.host.adapter.calls==1 and len(app_calls)==int(app_action=='step')
    finally:
        context.host.adapter.release.set()
        bus.off(Method.AUIP_ACTION_REQUESTED,app_receipt)
        await context.finish()


def test_retract_cannot_carry_an_export_or_missing_target():
    for action in ({'op':'work','intent':'retract'},
        {'op':'work','intent':'retract','target':'memo','external_export_target':'desktop'}):
        with pytest.raises(LoopConflict):
            _normalize_work_action(action)


async def test_mutation_report_batch_cannot_turn_withdrawal_into_new_work(loop_host):
    loop, adapter, controls, _, _ = loop_host
    text='便签先停一下，再说说游戏做到哪了。'
    controls[text]={'op':'batch','actions':[
        {'op':'work','intent':'retract','target':'便签','source':'便签先停一下'},
        {'op':'report','target':'游戏','source':'再说说游戏做到哪了。'}]}
    with pytest.raises(LoopConflict):
        await loop.submit(text)
    assert adapter.requests==[]
