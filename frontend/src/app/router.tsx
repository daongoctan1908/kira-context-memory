import { createBrowserRouter, Navigate, Outlet } from "react-router-dom";

import { AnonymousOnly, RequireAuth } from "../auth/AuthRoute";
import { AuthProvider } from "../auth/AuthProvider";
import { ChatLayout } from "../layouts/ChatLayout";
import { ChangePasswordPage } from "../pages/ChangePasswordPage";
import { ConversationPage } from "../pages/ConversationPage";
import { LoginPage } from "../pages/LoginPage";
import { NewChatPage } from "../pages/NewChatPage";
import { NotFoundPage } from "../pages/NotFoundPage";

export const appRoutes = [
  {
    element: (
      <AuthProvider>
        <Outlet />
      </AuthProvider>
    ),
    children: [
      {
        path: "/",
        element: <Navigate to="/chat/new" replace />,
      },
      {
        path: "/login",
        element: (
          <AnonymousOnly>
            <LoginPage />
          </AnonymousOnly>
        ),
      },
      {
        path: "/chat",
        element: (
          <RequireAuth>
            <ChatLayout />
          </RequireAuth>
        ),
        children: [
          {
            path: "new",
            element: <NewChatPage />,
          },
          {
            path: ":sessionId",
            element: <ConversationPage />,
          },
        ],
      },
      {
        path: "/account/password",
        element: (
          <RequireAuth>
            <ChangePasswordPage />
          </RequireAuth>
        ),
      },
      {
        path: "*",
        element: <NotFoundPage />,
      },
    ],
  },
];

export const router = createBrowserRouter(appRoutes);
