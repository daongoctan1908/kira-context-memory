import { Link } from "react-router-dom";

export function LoginPage() {
  return (
    <main className="centered-page" id="main-content">
      <section className="route-panel" aria-labelledby="login-title">
        <span className="brand-mark brand-mark-large" aria-hidden="true">
          K
        </span>
        <p className="route-kicker">KiRa Chat</p>
        <h1 id="login-title">Truy cập trợ lý nội bộ</h1>
        <p className="route-copy">Đăng nhập bằng tài khoản được quản trị viên cấp.</p>
        <Link className="secondary-link" to="/chat/new">
          Xem không gian trò chuyện
        </Link>
      </section>
    </main>
  );
}
