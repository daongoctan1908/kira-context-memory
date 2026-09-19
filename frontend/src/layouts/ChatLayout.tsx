import { NavLink, Outlet } from "react-router-dom";

export function ChatLayout() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <NavLink className="brand-link" to="/chat/new" aria-label="KiRa Chat, trang chính">
          <span className="brand-mark" aria-hidden="true">
            K
          </span>
          <span>KiRa Chat</span>
        </NavLink>
        <NavLink className="text-link" to="/login">
          Đăng nhập
        </NavLink>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <nav aria-label="Điều hướng cuộc trò chuyện">
            <NavLink className="new-chat-link" to="/chat/new">
              Cuộc trò chuyện mới
            </NavLink>
            <p className="sidebar-hint">Lịch sử trò chuyện sẽ xuất hiện tại đây.</p>
          </nav>
        </aside>
        <main className="content" id="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
