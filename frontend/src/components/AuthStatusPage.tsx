export function AuthStatusPage({
  mode,
  onRetry,
}: {
  mode: "loading" | "unavailable";
  onRetry?: () => void;
}) {
  return (
    <main className="centered-page" id="main-content">
      <section className="route-panel status-panel" aria-live="polite">
        <span className="brand-mark brand-mark-large" aria-hidden="true">
          K
        </span>
        {mode === "loading" ? (
          <>
            <h1>Đang kiểm tra phiên đăng nhập</h1>
            <p className="route-copy">Vui lòng chờ trong giây lát.</p>
          </>
        ) : (
          <>
            <h1>Chưa thể kết nối</h1>
            <p className="route-copy">
              Dịch vụ xác thực đang không khả dụng. Phiên hiện tại chưa bị thay đổi.
            </p>
            <button className="primary-button" type="button" onClick={onRetry}>
              Thử lại
            </button>
          </>
        )}
      </section>
    </main>
  );
}
