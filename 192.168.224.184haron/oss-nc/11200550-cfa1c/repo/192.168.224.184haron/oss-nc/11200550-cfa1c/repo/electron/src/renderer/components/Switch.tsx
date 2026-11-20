import { useI18n } from '../i18n'

/**
 * The one on/off control. `mixed` shows a value that is not known yet; activating it turns the value on.
 * Name it with `label`, or with a <label htmlFor={id}> as SettingsField does.
 */
export default function Switch({ checked, onChange, label, id, describedBy, disabled, stateText = true }: {
  checked: boolean | 'mixed'
  onChange: (checked: boolean) => void
  label?: string
  id?: string
  describedBy?: string
  disabled?: boolean
  /** Show On / Off beside the track. */
  stateText?: boolean
}) {
  const { t } = useI18n()
  return (
    <span className="switch-field">
      <button
        type="button"
        role="switch"
        id={id}
        className="switch"
        aria-checked={checked}
        aria-label={label}
        aria-describedby={describedBy}
        disabled={disabled}
        onClick={() => onChange(checked !== true)}
      >
        <span className="switch-thumb" aria-hidden="true" />
      </button>
      {stateText ? <span className="switch-state" aria-hidden="true">{t(checked === true ? 'On' : checked === false ? 'Off' : 'Unknown')}</span> : null}
    </span>
  )
}
