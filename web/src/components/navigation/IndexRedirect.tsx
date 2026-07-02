import { useContext } from "react";
import { Navigate } from "react-router-dom";
import { AuthContext } from "@/context/auth-context";
import ActivityIndicator from "@/components/indicators/activity-indicator";

export function IndexRedirect() {
  const { auth } = useContext(AuthContext);

  if (auth.isLoading) {
    return (
      <ActivityIndicator className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2" />
    );
  }

  const isAdmin = !auth.isAuthenticated || auth.user?.role === "admin";

  return <Navigate to={isAdmin ? "/config" : "/live"} replace />;
}
