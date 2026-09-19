import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/useAuth";
import { FormError } from "../components/FormError";
import { ConversationList } from "../conversations/ConversationList";
import { ConversationProvider } from "../conversations/ConversationProvider";

export function ChatLayout() {
  return (
    <ConversationProvider>
      <ChatWorkspace />
    </ConversationProvider>
  );
}

function ChatWorkspace() {
  const auth = useAuth();
  const [logoutError, setLogoutError] = useState<unknown>(null);
  const [loggingOut, setLoggingOut] = useState(false);

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
    <div className="app-shell">
      <header className="app-header">
        <NavLink className="brand-link" to="/chat/new" aria-label="KiRa Chat, trang chính">
          <span className="brand-mark" aria-hidden="true">
            K
          </span>
          <span>KiRa Chat</span>
        </NavLink>
        <div className="account-actions">
          <NavLink className="account-link" to="/account/password">
            <span className="account-name">{auth.user?.username}</span>
            <span>Đổi mật khẩu</span>
          </NavLink>
          <button
            className="text-button"
            type="button"
            disabled={loggingOut}
            onClick={() => void handleLogout()}
          >
            {loggingOut ? "Đang đăng xuất" : "Đăng xuất"}
          </button>
        </div>
      </header>

      {logoutError ? (
        <div className="header-error">
          <FormError error={logoutError} />
        </div>
      ) : null}

      <div className="workspace">
        <aside className="sidebar">
          <nav aria-label="Điều hướng cuộc trò chuyện">
            <NavLink className="new-chat-link" to="/chat/new">
              Cuộc trò chuyện mới
            </NavLink>
            <ConversationList />
          </nav>
        </aside>
        <main className="content" id="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
