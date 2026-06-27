import { useEffect } from "react";
import { Outlet, NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  Server,
  Layers,
  Play,
  PlusCircle,
  HardDrive,
  Shield,
  LogOut,
} from "lucide-react";
import { useAuthStore, handleWsMessage } from "@/stores";
import { useWebSocket } from "@/hooks/useWebSocket";
import { cn } from "@/lib/utils";

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard", end: true },
  { to: "/nodes", icon: Server, label: "Nodes" },
  { to: "/pools", icon: Layers, label: "Pools" },
  { to: "/jobs", icon: Play, label: "Jobs" },
  { to: "/jobs/new", icon: PlusCircle, label: "Job Builder" },
  { to: "/storage", icon: HardDrive, label: "Storage" },
  { to: "/admin", icon: Shield, label: "Admin" },
];

export function Layout() {
  const { user, isLoading, fetchUser, logout } = useAuthStore();
  const navigate = useNavigate();

  // Connect WebSocket for live updates
  useWebSocket(handleWsMessage);

  useEffect(() => {
    fetchUser();
  }, [fetchUser]);

  useEffect(() => {
    if (!isLoading && !user) {
      navigate("/login", { replace: true });
    }
  }, [isLoading, user, navigate]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-screen bg-background">
        <div className="text-muted-foreground text-lg">Loading...</div>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 bg-sidebar text-sidebar-foreground flex flex-col shrink-0 border-r border-border">
        {/* Logo */}
        <div className="px-6 py-5 border-b border-border">
          <h1 className="text-xl font-bold tracking-widest text-primary">NEXUS</h1>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {navItems.map(({ to, icon: Icon, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-colors",
                  isActive
                    ? "bg-primary/15 text-sidebar-active"
                    : "text-sidebar-foreground hover:bg-primary/10 hover:text-foreground"
                )
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* User info + logout */}
        <div className="px-4 py-4 border-t border-border">
          <div className="flex items-center justify-between">
            <div className="min-w-0">
              <p className="text-sm font-medium truncate text-foreground">{user.username}</p>
              <p className="text-xs text-muted-foreground capitalize">{user.role}</p>
            </div>
            <button
              onClick={() => {
                logout();
                navigate("/login", { replace: true });
              }}
              className="p-2 rounded-md text-muted-foreground hover:text-foreground hover:bg-primary/10 transition-colors"
              title="Log out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 bg-background overflow-y-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}
