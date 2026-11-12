import type {RiskProfileCatalogue, Scalar} from '../api/types'

const profileLabel = (name: string) => name.charAt(0) + name.slice(1).toLowerCase()

function displayValue(value: Scalar | undefined, type: string) {
  if (type === 'boolean') return value ? 'Yes' : 'No'
  return value === null || value === undefined ? '—' : String(value)
}

/** Three-way risk profile choice and a read-only summary of what it sets.
 *
 * Every number comes from the Backend catalogue; nothing here is a preset. */
export function RiskProfileSelector({catalogue, value, onChange, disabled = false}: {
  catalogue: RiskProfileCatalogue
  value: string
  onChange: (profile: string) => void
  disabled?: boolean
}) {
  const selected = catalogue.profiles[value]
  return <div className="risk-profile">
    <div className="risk-profile-options" role="radiogroup" aria-label="Risk profile">
      {catalogue.profile_names.map((name) => <button
        key={name}
        type="button"
        role="radio"
        aria-checked={name === value}
        className={name === value ? 'button-primary' : 'button-secondary'}
        disabled={disabled}
        onClick={() => onChange(name)}
      >{profileLabel(name)}</button>)}
    </div>
    <div className="review-grid" aria-label="Risk profile summary">
      {catalogue.summary_fields.map((field) => <div key={field.key}>
        <span>{field.label}</span>
        <strong>{displayValue(selected?.summary[field.key], field.type)}</strong>
      </div>)}
    </div>
    <p className="inline-note">Every other strategy parameter is fixed by the platform and is the same in all three profiles.</p>
  </div>
}

export {profileLabel}
