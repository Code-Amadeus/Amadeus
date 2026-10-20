import type { WorkPermissionProjection } from './types'
import type { PermissionAction } from './permissionProjection'

interface Props {
  request: WorkPermissionProjection | null
  grants: WorkPermissionProjection[]
  automatic?: boolean
  busy: boolean
  error: string
  onDecision: (permission: WorkPermissionProjection, action: PermissionAction) => void
  onSetAutomatic?: (enabled: boolean) => void
  t: (value: string) => string
}

export default function WorkPermissionsPanel({ request, grants, automatic = false, busy, error, onDecision, onSetAutomatic, t }: Props) {
  if (!request && !grants.length && !automatic) return null
  const callDetails = (permission: WorkPermissionProjection) => permission.mcpCall && (
    <>
      <p>{t('Only this connection, tool and exactly the same arguments; expires when this Work ends or is cancelled.')}</p>
      <code>{permission.mcpCall.connectionId} / {permission.mcpCall.toolName}</code>
      <pre style={{ maxHeight: 160, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{JSON.stringify(permission.mcpCall.arguments, null, 2)}</pre>
    </>
  )
  return (
    <section className="crt-side-card permission" aria-label={t('Work permissions')}>
      {automatic && <div>
        <p>{t('Automatic approval is on for Work and Cooperative chats, including MCP and shell. Permission limits still apply. It stays on until turned off.')}</p>
        {onSetAutomatic && <button disabled={busy} onClick={() => onSetAutomatic(false)}>{t('Turn off automatic approval')}</button>}
      </div>}
      {request && <div>
        <strong>{t('Approval required')}</strong>
        <p>{request.reason}</p>
        {request.scope.map(scope => <p key={scope}><code>{scope}</code></p>)}
        {callDetails(request)}
        <div className="crt-side-actions">
          {request.options.includes('allow_once') && <button disabled={busy} onClick={() => onDecision(request, 'allow_once')}>{t('Allow once')}</button>}
          {request.options.includes('allow_work') && request.mcpCall && <button disabled={busy} onClick={() => onDecision(request, 'allow_work')}>{t('Allow identical calls for this Work')}</button>}
          {request.options.includes('deny') && <button disabled={busy} onClick={() => onDecision(request, 'deny')}>{t('Deny')}</button>}
        </div>
        {!automatic && onSetAutomatic && <div>
          <p>{t('Automatically approve permitted requests in all Work and Cooperative chats, including MCP and shell. Saved across restarts until you turn it off; approvals remain recorded.')}</p>
          <button disabled={busy} onClick={() => {
            if (window.confirm(t('Enable automatic approval for all Work and Cooperative chats, including MCP and shell? This also approves eligible pending requests and stays on across restarts until you turn it off. Permission limits still apply.'))) {
              onSetAutomatic(true)
            }
          }}>{t('Always automatically approve Work and Chat')}</button>
        </div>}
      </div>}
      {grants.length > 0 && <strong>{t('Active Work permissions')}</strong>}
      {grants.map(grant => <div key={grant.id}>
        {callDetails(grant)}
        <button disabled={busy} onClick={() => onDecision(grant, 'revoke_work')}>{t('Revoke')}</button>
      </div>)}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
