import { useState, type SyntheticEvent } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/useAuth";
import { FormError } from "../components/FormError";

export function ChangePasswordPage() {
  const auth = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const confirmationMismatch = confirmation.length > 0 && newPassword !== confirmation;

  const handleSubmit = async (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (newPassword !== confirmation) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await auth.changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
    } catch (caught) {
      setError(caught);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="centered-page" id="main-content">
      <section className="route-panel auth-panel" aria-labelledby="change-password-title">
        <p className="route-kicker">Tài khoản {auth.user?.username}</p>
        <h1 id="change-password-title">Đổi mật khẩu</h1>
        <p className="route-copy">
          Sau khi đổi, tất cả phiên đăng nhập hiện tại sẽ bị thu hồi.
        </p>
        <form
          className="auth-form"
          onSubmit={(event) => {
            void handleSubmit(event);
          }}
        >
          <div className="field-group">
            <label htmlFor="current-password">Mật khẩu hiện tại</label>
            <input
              id="current-password"
              name="current-password"
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => {
                setCurrentPassword(event.target.value);
              }}
              maxLength={128}
              required
              autoFocus
            />
          </div>
          <div className="field-group">
            <label htmlFor="new-password">Mật khẩu mới</label>
            <input
              id="new-password"
              name="new-password"
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => {
                setNewPassword(event.target.value);
              }}
              minLength={12}
              maxLength={128}
              aria-describedby="new-password-help"
              required
            />
            <span className="field-help" id="new-password-help">
              Dùng từ 12 đến 128 ký tự.
            </span>
          </div>
          <div className="field-group">
            <label htmlFor="confirm-password">Nhập lại mật khẩu mới</label>
            <input
              id="confirm-password"
              name="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirmation}
              onChange={(event) => {
                setConfirmation(event.target.value);
              }}
              minLength={12}
              maxLength={128}
              aria-invalid={confirmationMismatch}
              aria-describedby={confirmationMismatch ? "password-mismatch" : undefined}
              required
            />
            {confirmationMismatch ? (
              <span className="field-error" id="password-mismatch">
                Mật khẩu nhập lại chưa khớp.
              </span>
            ) : null}
          </div>
          {error ? <FormError error={error} /> : null}
          <div className="form-actions">
            <button
              className="primary-button"
              type="submit"
              disabled={submitting || confirmationMismatch}
            >
              {submitting ? "Đang đổi mật khẩu" : "Đổi mật khẩu"}
            </button>
            <Link className="secondary-link" to="/chat/new">
              Quay lại
            </Link>
          </div>
        </form>
      </section>
    </main>
  );
}
