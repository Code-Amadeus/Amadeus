from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITUATIONS = ROOT / "sdk" / "auip-core" / "situations-v0.js"


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if not node:
        print("ok: AUIP situation test skipped (node unavailable)")
        return
    result = subprocess.run(
        [node, "-e", script, str(SITUATIONS)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_grid_situation_preserves_direct_coordinate_and_row_structure() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {gridSituation} = require(process.argv[1]);
const cells = [
  ['b', null, null],
  [null, 'w', null],
];
const grid = gridSituation({
  width: 3,
  height: 2,
  empty: '.',
  legend: {b:'black', w:'white'},
  cell:(x,y)=>cells[y][x] || '.',
});
assert.deepEqual(grid, {
  kind:'grid/v1', width:3, height:2, empty:'.',
  legend:{b:'black', w:'white'}, rows:['b..', '.w.'],
});
assert.equal(grid.rows[1][1], 'w');
assert.equal(Object.isFrozen(grid), true);
assert.equal(Object.isFrozen(grid.rows), true);
"""
    )


def test_grid_situation_rejects_shape_and_symbol_drift() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {gridSituation} = require(process.argv[1]);
assert.throws(
  ()=>gridSituation({width:0,height:2,legend:{b:'black'},cell:()=>'.'}),
  error=>error.code === 'grid_dimension_invalid'
);
assert.throws(
  ()=>gridSituation({width:2,height:2,legend:{b:'black'},cell:()=> 'w'}),
  error=>error.code === 'grid_symbol_not_in_legend'
);
assert.throws(
  ()=>gridSituation({width:2,height:2,legend:{'bb':'black'},cell:()=>'.'}),
  error=>error.code === 'grid_symbol_invalid'
);
"""
    )


def test_choice_situation_preserves_exact_payloads_and_availability() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {choiceSituation} = require(process.argv[1]);
const payload = {source:'A', channel:'red'};
const situation = choiceSituation({options:[
  {id:'a-red',label:'A to red',action:'game.connect',payload,available:true},
  {id:'b-red',label:'B to red',action:'game.connect',payload:{source:'B',channel:'red'},available:false},
]});
assert.deepEqual(situation, {
  kind:'choice/v1',
  options:[
    {id:'a-red',label:'A to red',action:'game.connect',payload:{source:'A',channel:'red'},available:true},
    {id:'b-red',label:'B to red',action:'game.connect',payload:{source:'B',channel:'red'},available:false},
  ],
});
assert.notEqual(situation.options[0].payload, payload);
assert.equal(Object.isFrozen(situation.options[0].payload), true);
assert.deepEqual(payload, {source:'A',channel:'red'});
const lifecycle = choiceSituation({
  actionTypes:['run.start','run.continue','meta.upgrade'],
  options:[
    {id:'restart',label:'Restart',action:'run.start',payload:{},available:true},
    {id:'continue',label:'Continue',action:'run.continue',payload:{},available:false},
  ],
});
assert.deepEqual(lifecycle.actionTypes, [
  'run.start','run.continue','meta.upgrade',
]);
assert.throws(
  ()=>choiceSituation({
    actionTypes:['run.start'],
    options:[{id:'continue',label:'Continue',action:'run.continue',payload:{},available:true}],
  }),
  error=>error.code === 'choice_option_action_not_governed'
);
assert.throws(
  ()=>choiceSituation({options:[
    {id:'same',label:'one',action:'game.connect',payload:{x:1},available:true},
    {id:'same',label:'two',action:'game.connect',payload:{x:2},available:true},
  ]}),
  error=>error.code === 'choice_option_duplicate'
);
"""
    )


def test_compact_choice_situation_deduplicates_wire_fields_not_payloads() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {choiceSituation} = require(process.argv[1]);
const options = [];
for (const source of ['A','B','C']) {
  for (const channel of ['red','green','blue']) {
    options.push({
      id:source+channel,
      label:source+'→'+channel,
      payload:{source,channel},
      available:true,
    });
  }
}
const situation = choiceSituation({
  compact:true,
  action:'game.connect',
  options,
});
assert.equal(situation.kind, 'choice/v1');
assert.equal(situation.action, 'game.connect');
assert.deepEqual(situation.options[0], {
  label:'A→red', payload:{source:'A',channel:'red'},
});
assert.equal('id' in situation.options[0], false);
assert.equal(JSON.stringify(situation).length < 720, true);
assert.throws(
  ()=>choiceSituation({
    compact:true,
    action:'game.connect',
    options:[{id:'x',label:'X',payload:{x:1},available:false}],
  }),
  error=>error.code === 'choice_compact_unavailable_option'
);
assert.throws(
  ()=>choiceSituation({
    compact:true,
    action:'game.connect',
    options:[{id:'x',label:'X',action:'game.reset',payload:{},available:true}],
  }),
  error=>error.code === 'choice_compact_action_mismatch'
);
"""
    )


def test_action_addressed_choice_uses_manifest_actions_as_portable_labels() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {choiceSituation} = require(process.argv[1]);
const situation = choiceSituation({
  actionAddressed:true,
  actionTypes:['game.resign','game.restart_round','game.finish_experience'],
  options:[
    {id:'r',label:'resign',action:'game.resign',payload:{},available:true},
    {id:'n',label:'restart',action:'game.restart_round',payload:{},available:false},
    {id:'x',label:'finish',action:'game.finish_experience',payload:{},available:false},
  ],
});
assert.deepEqual(situation, {
  kind:'choice/v1',
  options:[
    {action:'game.resign',payload:{},available:true},
    {action:'game.restart_round',payload:{},available:false},
    {action:'game.finish_experience',payload:{},available:false},
  ],
  actionTypes:['game.resign','game.restart_round','game.finish_experience'],
});
assert.equal(JSON.stringify(situation).length < 320, true);
assert.throws(
  ()=>choiceSituation({
    compact:true,
    actionAddressed:true,
    action:'game.move',
    options:[{id:'a',label:'A',payload:{},available:true}],
  }),
  error=>error.code === 'choice_projection_modes_conflict'
);
assert.throws(
  ()=>choiceSituation({
    actionAddressed:true,
    options:[
      {id:'a',label:'A',action:'game.move',payload:{x:1},available:true},
      {id:'b',label:'B',action:'game.move',payload:{x:1},available:false},
    ],
  }),
  error=>error.code === 'choice_action_payload_duplicate'
);
"""
    )


def test_action_availability_situation_exposes_one_stable_action_family() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {actionAvailabilitySituation} = require(process.argv[1]);
const situation = actionAvailabilitySituation({
  actionTypes:['game.place','game.take_first'],
  availableActionTypes:['game.take_first'],
});
assert.deepEqual(situation, {
  kind:'action_availability/v1',
  actionTypes:['game.place','game.take_first'],
  availableActionTypes:['game.take_first'],
});
assert.equal(Object.isFrozen(situation.availableActionTypes), true);
assert.throws(
  ()=>actionAvailabilitySituation({
    actionTypes:['game.place'],
    availableActionTypes:['game.take_first'],
  }),
  error=>error.code === 'available_action_type_not_governed'
);
assert.throws(
  ()=>actionAvailabilitySituation({
    actionTypes:['game.place','game.place'],
    availableActionTypes:[],
  }),
  error=>error.code === 'action_availability_type_duplicate'
);
"""
    )


def test_scalar_situation_preserves_value_trend_and_safe_range() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {scalarSituation} = require(process.argv[1]);
const situation = scalarSituation({metrics:[
  {id:'temperature',label:'Core temperature',value:86,unit:'°C',trend:'falling',safe:[45,55]},
]});
assert.deepEqual(situation, {
  kind:'scalars/v1',
  metrics:[{id:'temperature',label:'Core temperature',value:86,unit:'°C',trend:'falling',safe:[45,55]}],
});
assert.equal(Object.isFrozen(situation.metrics[0].safe), true);
assert.throws(
  ()=>scalarSituation({metrics:[
    {id:'temperature',label:'Core',value:50,unit:'°C',trend:'unknown',safe:[45,55]},
  ]}),
  error=>error.code === 'scalar_trend_invalid'
);
assert.throws(
  ()=>scalarSituation({metrics:[
    {id:'temperature',label:'Core',value:50,unit:'°C',trend:'steady',safe:[55,45]},
  ]}),
  error=>error.code === 'scalar_safe_range_invalid'
);
"""
    )


def test_sequence_situation_preserves_order_and_names_the_next_step() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {sequenceSituation} = require(process.argv[1]);
const situation = sequenceSituation({
  steps:[
    {id:'power',label:'Connect power'},
    {id:'calibrate',label:'Calibrate guidance'},
    {id:'pressurize',label:'Pressurize fuel'},
  ],
  completedCount:1,
});
assert.deepEqual(situation, {
  kind:'sequence/v1',
  completedCount:1,
  nextStepId:'calibrate',
  steps:[
    {id:'power',label:'Connect power'},
    {id:'calibrate',label:'Calibrate guidance'},
    {id:'pressurize',label:'Pressurize fuel'},
  ],
});
assert.equal(Object.isFrozen(situation), true);
assert.equal(Object.isFrozen(situation.steps), true);
assert.equal(Object.isFrozen(situation.steps[0]), true);
assert.equal(sequenceSituation({
  steps:[{id:'one',label:'One'}], completedCount:1,
}).nextStepId, null);
assert.throws(
  ()=>sequenceSituation({
    steps:[{id:'same',label:'One'},{id:'same',label:'Two'}], completedCount:0,
  }),
  error=>error.code === 'sequence_step_duplicate'
);
assert.throws(
  ()=>sequenceSituation({steps:[{id:'one',label:'One'}],completedCount:2}),
  error=>error.code === 'sequence_completed_count_invalid'
);
"""
    )


def test_controller_situation_exposes_governance_without_rewriting_policy() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {controllerSituation} = require(process.argv[1]);
const active = controllerSituation({
  status:'active',
  policyRevision:7,
  policyAction:'vehicle.set_navigation_policy',
  policySummary:'Dock at A-12 with soft capture',
});
assert.deepEqual(active, {
  kind:'controller/v1',
  status:'active',
  policyRevision:7,
  policyAction:'vehicle.set_navigation_policy',
  policySummary:'Dock at A-12 with soft capture',
});
assert.equal('policy' in active, false);
assert.equal(Object.isFrozen(active), true);
assert.deepEqual(controllerSituation({
  status:'idle', policyRevision:null, policyAction:null, policySummary:'',
  reason:'user takeover',
}), {
  kind:'controller/v1', status:'idle', policyRevision:null,
  policyAction:null, policySummary:'', reason:'user takeover',
});
assert.throws(
  ()=>controllerSituation({
    status:'active', policyRevision:1, policyAction:'follow', policySummary:'Follow',
  }),
  error=>error.code === 'choice_action_invalid'
);
assert.throws(
  ()=>controllerSituation({
    status:'idle', policyRevision:1, policyAction:'app.policy', policySummary:'x',
  }),
  error=>error.code === 'controller_idle_policy_invalid'
);
"""
    )
