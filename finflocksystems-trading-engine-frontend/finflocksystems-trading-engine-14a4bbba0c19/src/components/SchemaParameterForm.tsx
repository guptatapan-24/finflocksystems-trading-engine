import type {ParameterSchema, Scalar} from '../api/types'


function fieldValue(value: Scalar | undefined) {
  return value === null || value === undefined ? '' : String(value)
}

function parseParameter(value: string, type?: string): Scalar {
  if (type === 'integer' || type === 'number') return value === '' ? null : Number(value)
  if (type === 'boolean') return value === 'true'
  return value
}

export function SchemaParameterForm({schema, values, onChange, ariaPrefix = '', fixedValues = {}}: {
  schema: ParameterSchema
  values: Record<string, Scalar>
  onChange: (values: Record<string, Scalar>) => void
  ariaPrefix?: string
  fixedValues?: Record<string, Scalar>
}) {
  const properties = Object.entries(schema.properties || {})
  if (!properties.length) return <p className="inline-note">This strategy has no configurable parameters.</p>
  return <div className="form-grid two-columns">{properties.map(([key, property]) => {
    const label = property.title || key.replaceAll('_', ' ')
    const ariaLabel = `${ariaPrefix}${label}`.trim()
    const fixed = Object.prototype.hasOwnProperty.call(fixedValues, key)
    const currentValue = fixed ? fixedValues[key] : values[key]
    const set = (value: Scalar) => onChange({...values, ...fixedValues, [key]: value})
    return <label key={key}>{label}{property.description && <small>{property.description}</small>}
      {property.enum ? <select aria-label={ariaLabel} disabled={fixed} value={fieldValue(currentValue)} onChange={(event) => set(parseParameter(event.target.value, property.type))}>{(fixed ? [currentValue] : property.enum).map((value) => <option key={String(value)} value={String(value)}>{String(value)}</option>)}</select>
        : property.type === 'boolean' ? <select aria-label={ariaLabel} disabled={fixed} value={fieldValue(currentValue)} onChange={(event) => set(event.target.value === 'true')}><option value="true">Yes</option><option value="false">No</option></select>
          : <input aria-label={ariaLabel} readOnly={fixed} type={property.type === 'integer' || property.type === 'number' ? 'number' : 'text'} step={property.type === 'integer' ? 1 : 'any'} min={property.minimum ?? property.exclusiveMinimum} max={property.maximum ?? property.exclusiveMaximum} value={fieldValue(currentValue)} onChange={(event) => set(parseParameter(event.target.value, property.type))} />}
    </label>
  })}</div>
}
