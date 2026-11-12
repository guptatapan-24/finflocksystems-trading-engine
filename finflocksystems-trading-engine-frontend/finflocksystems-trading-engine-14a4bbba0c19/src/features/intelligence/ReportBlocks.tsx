import {EmptyState, TerminalPanel} from '../../components/ui'
import type {ReportBlock} from './types'

export function Block({block}: {block: ReportBlock}) {
  if (block.type === 'text') return <p>{block.text}</p>
  if (block.type === 'list') return <ul>{block.items.map((item, index) => <li key={index}>{item}</li>)}</ul>
  if (block.type === 'kv') return <dl className="detail-list">{block.rows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
  if (!block.rows.length) return <EmptyState title="Nothing to show" />
  return <div className="data-table-wrap"><table className="data-table">
    <thead><tr>{block.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
    <tbody>{block.rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{String(cell)}</td>)}</tr>)}</tbody>
  </table></div>
}

export function ReportSections({sections, idPrefix = 'intelligence-report'}: {sections: {id: string; title: string; blocks: ReportBlock[]}[]; idPrefix?: string}) {
  return <>{sections.map((section) => <TerminalPanel key={section.id} id={`${idPrefix}-${section.id}`} title={section.title}>
    {section.blocks.map((block, index) => <Block key={index} block={block} />)}
  </TerminalPanel>)}</>
}
