import { NavLink, Route, Routes } from 'react-router-dom';
import { Dashboard } from './pages/Dashboard';
import { NewRun } from './pages/NewRun';
import { RunDetail } from './pages/RunDetail';
import { Datasets } from './pages/Datasets';
import { DatasetDetail } from './pages/DatasetDetail';
import { LineagePage } from './pages/LineagePage';
import { System } from './pages/System';

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <li>
      <NavLink to={to} end className={({ isActive }) => (isActive ? 'active' : '')}>
        {label}
      </NavLink>
    </li>
  );
}

export function App() {
  return (
    <div className="app-shell">
      <nav className="app-nav" aria-label="Main navigation">
        <h1>Forge Data</h1>
        <ul>
          <NavItem to="/" label="Dashboard" />
          <NavItem to="/runs/new" label="New Run" />
          <NavItem to="/datasets" label="Datasets" />
          <NavItem to="/lineage" label="Lineage" />
          <NavItem to="/system" label="System" />
        </ul>
      </nav>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/runs/new" element={<NewRun />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/datasets" element={<Datasets />} />
          <Route path="/datasets/:datasetName" element={<DatasetDetail />} />
          <Route path="/lineage" element={<LineagePage />} />
          <Route path="/system" element={<System />} />
          <Route path="*" element={<Dashboard />} />
        </Routes>
      </main>
    </div>
  );
}
