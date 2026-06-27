import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Layout } from "@/components/Layout";
import LoginPage from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import Nodes from "@/pages/Nodes";
import Pools from "@/pages/Pools";
import Jobs from "@/pages/Jobs";
import JobBuilder from "@/pages/JobBuilder";
import JobDetail from "@/pages/JobDetail";
import Storage from "@/pages/Storage";
import Admin from "@/pages/Admin";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="nodes" element={<Nodes />} />
          <Route path="pools" element={<Pools />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="jobs/new" element={<JobBuilder />} />
          <Route path="jobs/:id" element={<JobDetail />} />
          <Route path="storage" element={<Storage />} />
          <Route path="admin" element={<Admin />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
