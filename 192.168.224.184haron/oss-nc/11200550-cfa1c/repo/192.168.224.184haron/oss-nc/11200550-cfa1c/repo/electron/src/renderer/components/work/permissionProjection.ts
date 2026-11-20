import type { WorkPermissionProjection } from './types'

export type PermissionAction = 'allow_once' | 'allow_work' | 'deny' | 'revoke_work'

export function permissionActionPayload(permission: WorkPermissionProjection, action: PermissionAction, revision: string): Record<string, unknown> {
  return {
    action, permission_request_id: permission.id, work_item_id: permission.workItemId,
    attempt_id: permission.attemptId, revision, surface: 'electron.work',
  }
}
