import {NavLink} from 'react-router-dom'

// Sub-navigation shared by the intelligence pages (one sidebar item, several views).
export function IntelligenceTabs() {
  const link = ({isActive}: {isActive: boolean}) => (isActive ? 'active' : '')
  return <div className="tabs" role="tablist" aria-label="Intelligence views">
    <NavLink to="/intelligence/episodes" className={link} role="tab">Trade episodes</NavLink>
    <NavLink to="/intelligence/live" className={link} role="tab">Live trades</NavLink>
    <NavLink to="/intelligence/sessions" className={link} role="tab">Sessions</NavLink>
    <NavLink to="/intelligence/learning" className={link} role="tab">Learning</NavLink>
  </div>
}
