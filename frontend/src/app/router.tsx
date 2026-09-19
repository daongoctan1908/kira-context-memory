import { createBrowserRouter, Navigate } from "react-router-dom";

import { ChatLayout } from "../layouts/ChatLayout";
import { ConversationPage } from "../pages/ConversationPage";
import { LoginPage } from "../pages/LoginPage";
import { NewChatPage } from "../pages/NewChatPage";
import { NotFoundPage } from "../pages/NotFoundPage";

export const appRoutes = [
  {
    path: "/",
    element: <Navigate to="/chat/new" replace />,
  },
  {
    path: "/login",
    element: <LoginPage />,
  },
  {
    path: "/chat",
    element: <ChatLayout />,
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
    path: "*",
    element: <NotFoundPage />,
  },
];

export const router = createBrowserRouter(appRoutes);
