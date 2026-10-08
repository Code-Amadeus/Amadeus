import { useEffect, useState } from 'react'
import { settingSourceLabel, type BackendStartupFailure } from '../../shared/characterStartup'
import { useI18n } from '../i18n'
import { CardShell } from './SettingsPrimitives'

interface Props {
  connected: boolean
  restarting: boolean
  reconnectBackend: () => Promise<void>
  onSettingsChanged: (settings: Record<string, unknown>) => void
}

export function StartupFailureCard({ failure, recovering, error, notice, onRecover }: {
  failure: BackendStartupFailure; recovering: boolean; error: string; notice: string; onRecover: () => void
}) {
  const { t } = useI18n()
  return <CardShell vertical>
    <div className="character-prompt-editor" role="alert">
      <div className="settings-field-label">{t(failure.kind === 'character' ? 'Selected startup role could not be loaded.' : 'Backend could not start.')}</div>
      <div className="settings-field-description">{failure.kind === 'character'
        ? t('The selected role is missing or invalid. Its file and your other settings have been kept.') : failure.detail}</div>
      {failure.kind === 'character' ? <>
        <div className="settings-field-description">{t('Startup selection source: {source}', { source: t(settingSourceLabel(failure.selection.source)) })}
          {failure.selection.characterId ? ` · ${failure.selection.characterId}` : ''}</div>
        {failure.selection.locked ? <div className="settings-field-description">{t('Change AMADEUS_CHARACTER_ID in the parent process environment, then reopen Amadeus. Desktop recovery is locked by that source.')}</div> : null}
        <div className="character-prompt-actions"><button type="button" disabled={recovering || failure.selection.locked} onClick={onRecover}>{t(recovering ? 'Restarting…' : 'Use built-in Kurisu and restart')}</button></div>
      </> : null}
      {notice ? <div role="status" className="settings-field-description">{t(notice)}</div> : null}
      {error ? <div className="settings-field-description">{t(error)}</div> : null}
    </div>
  </CardShell>
}

export default function BackendStartupRecovery({ connected, restarting, reconnectBackend, onSettingsChanged }: Props) {
  const { t } = useI18n()
  const [failure, setFailure] = useState<BackendStartupFailure | null>(null)
  const [recovering, setRecovering] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => {
    if (connected) { setFailure(null); return }
    let current = true
    void window.amadeus?.getBackendStartupFailure().then(value => { if (current) setFailure(value) })
      .catch(reason => { if (current) setError(reason instanceof Error ? reason.message : 'Could not read startup status.') })
    return () => { current = false }
  }, [connected, restarting])

  const recover = async () => {
    if (!window.amadeus) return
    setRecovering(true)
    setError('')
    setNotice('')
    try {
      const result = await window.amadeus.recoverCharacterStartup()
      if (result.settings) onSettingsChanged(result.settings)
      if (!result.ok) {
        if (result.saved) setNotice('Built-in Kurisu was saved, but backend restart failed.')
        setFailure(result.failure || await window.amadeus.getBackendStartupFailure())
        throw new Error(result.error || 'Backend restart failed')
      }
      setFailure(null)
      setNotice('Backend restarted; reconnecting to confirm saved settings…')
      await reconnectBackend()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Backend restart failed')
    } finally { setRecovering(false) }
  }

  return failure ? <StartupFailureCard failure={failure} recovering={recovering} error={error} notice={notice} onRecover={() => void recover()} />
    : error ? <CardShell vertical><div role="alert" className="settings-field-description">{notice ? <div>{t(notice)}</div> : null}{t(error)}</div></CardShell> : null
}
