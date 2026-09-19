import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <main className="centered-page" id="main-content">
      <section className="route-panel" aria-labelledby="not-found-title">
        <p className="route-kicker">Không tìm thấy trang</p>
        <h1 id="not-found-title">Đường dẫn không tồn tại</h1>
        <p className="route-copy">Quay lại không gian trò chuyện để tiếp tục.</p>
        <Link className="primary-link" to="/chat/new">
          Mở cuộc trò chuyện mới
        </Link>
      </section>
    </main>
  );
}
