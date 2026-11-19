exports.onExecutePostChangePassword = async (event) => {
  const connection = event.secrets.CREDIT_AUTH0_CONNECTION;
  if (connection && event.connection.name !== connection) return;
  const origin = event.secrets.CREDIT_APP_ORIGIN;
  const secret = event.secrets.CREDIT_AUTH0_PASSWORD_HOOK_SECRET;
  if (!origin || !secret) throw new Error("Password-change delivery is not configured");

  const destination = new URL("/auth/password-changed", origin);
  if (destination.protocol !== "https:") throw new Error("Password-change delivery requires HTTPS");
  const response = await fetch(destination, {
    method: "POST",
    headers: {"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
    body: JSON.stringify({subject: event.user.user_id}),
    redirect: "error",
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error("Password-change delivery failed with HTTP " + response.status);
};
