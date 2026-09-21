import {
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  SquarePen,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/useAuth";
import { AccountMenu } from "../components/AccountMenu";
import { FormError } from "../components/FormError";
import { ConversationActions } from "../conversations/ConversationActions";
import { ConversationList } from "../conversations/ConversationList";
import { ConversationProvider } from "../conversations/ConversationProvider";
import { ConversationSearchDialog } from "../conversations/ConversationSearchDialog";
import { conversationTitle } from "../conversations/conversationLabels";
import { useConversations } from "../conversations/useConversations";

const SIDEBAR_STORAGE_KEY = "kira-sidebar-collapsed";

export function ChatLayout() {
  return (
    <ConversationProvider>
      <ChatWorkspace />
    </ConversationProvider>
  );
}

function ChatWorkspace() {
  const auth = useAuth();
  const conversations = useConversations();
  const location = useLocation();
  const [logoutError, setLogoutError] = useState<unknown>(null);
  const [loggingOut, setLoggingOut] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [online, setOnline] = useState(() => navigator.onLine);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "true",
  );
  const sessionId = readSessionId(location.pathname);
  const current = useMemo(
    () => conversations.conversations.find((item) => item.session_id === sessionId),
    [conversations.conversations, sessionId],
  );
  const pageTitle = sessionId === null
    ? "Cuộc trò chuyện mới"
    : current === undefined ? "Cuộc trò chuyện" : conversationTitle(current);

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => { window.removeEventListener("keydown", handleShortcut); };
  }, []);

  useEffect(() => {
    if (!sidebarOpen) {
      return;
    }
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [sidebarOpen]);

  useEffect(() => {
    const markOnline = () => { setOnline(true); };
    const markOffline = () => { setOnline(false); };
    window.addEventListener("online", markOnline);
    window.addEventListener("offline", markOffline);
    return () => {
      window.removeEventListener("online", markOnline);
      window.removeEventListener("offline", markOffline);
    };
  }, []);

  const handleLogout = async () => {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await auth.logout();
    } catch (error) {
      setLogoutError(error);
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <div className={sidebarCollapsed ? "app-shell sidebar-collapsed" : "app-shell"}>
      <a className="skip-link" href="#main-content">Bỏ qua tới nội dung chính</a>
      {sidebarOpen ? (
        <button className="sidebar-backdrop" type="button" aria-label="Đóng nền thanh bên" onClick={() => { setSidebarOpen(false); }} />
      ) : null}
      <aside className={sidebarOpen ? "sidebar open" : "sidebar"} id="conversation-sidebar">
        <div className="sidebar-brand-row">
          <NavLink className="brand-link" to="/chat/new" aria-label="KiRa Chat, trang chính" onClick={() => { setSidebarOpen(false); }}>
            <span className="brand-mark" aria-hidden="true">K</span>
            <span className="brand-copy">KiRa</span>
          </NavLink>
          <button
            className="icon-button desktop-sidebar-toggle"
            type="button"
            aria-label={sidebarCollapsed ? "Mở rộng thanh bên" : "Thu gọn thanh bên"}
            title={sidebarCollapsed ? "Mở rộng thanh bên" : "Thu gọn thanh bên"}
            onClick={() => { setSidebarCollapsed((value) => !value); }}
          >
            {sidebarCollapsed ? <PanelLeftOpen size={19} /> : <PanelLeftClose size={19} />}
          </button>
          <button
            className="icon-button mobile-sidebar-close"
            type="button"
            aria-label="Đóng danh sách cuộc trò chuyện"
            onClick={() => { setSidebarOpen(false); }}
          >
            <X size={20} />
          </button>
        </div>
        <nav className="sidebar-navigation" aria-label="Điều hướng cuộc trò chuyện" onClick={(event) => {
          if ((event.target as HTMLElement).closest("a") !== null) setSidebarOpen(false);
        }}>
          <NavLink className="sidebar-primary-action" to="/chat/new" title="Cuộc trò chuyện mới">
            <SquarePen size={19} />
            <span>Cuộc trò chuyện mới</span>
          </NavLink>
          <button className="sidebar-search-action" type="button" title="Tìm kiếm" onClick={() => { setSearchOpen(true); }}>
            <Search size={18} />
            <span>Tìm kiếm</span>
            <kbd>Ctrl K</kbd>
          </button>
          <ConversationList />
        </nav>
        <AccountMenu loggingOut={loggingOut} onLogout={() => void handleLogout()} />
      </aside>

      <div className="main-panel">
        <header className="main-header">
          <button
            className="icon-button mobile-sidebar-toggle"
            type="button"
            aria-label="Mở danh sách cuộc trò chuyện"
            aria-expanded={sidebarOpen}
            aria-controls="conversation-sidebar"
            onClick={() => { setSidebarOpen(true); }}
          >
            <Menu size={20} />
          </button>
          <h1>{pageTitle}</h1>
          <div className="main-header-actions">
            {current !== undefined ? <ConversationActions conversation={current} /> : null}
          </div>
        </header>
        {!online ? (
          <div className="network-status" role="status">
            Bạn đang ngoại tuyến. Bản nháp vẫn được giữ trên thiết bị.
          </div>
        ) : null}
        {logoutError ? <div className="header-error"><FormError error={logoutError} /></div> : null}
        <main className="content" id="main-content"><Outlet /></main>
      </div>
      <ConversationSearchDialog
        open={searchOpen}
        recent={conversations.conversations}
        onOpenChange={setSearchOpen}
      />
    </div>
  );
}

function readSessionId(pathname: string): string | null {
  const match = /^\/chat\/([^/]+)$/.exec(pathname);
  if (match?.[1] === undefined || match[1] === "new") {
    return null;
  }
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}
