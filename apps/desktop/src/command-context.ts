import type { CommandContext } from "./contracts";

function identifier(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}:${suffix}`;
}

export function createCommandContextFactory(actorRef = "local:user") {
  const clientInstanceId = identifier("browser");
  return (): CommandContext => ({
    actorRef,
    clientInstanceId,
    requestId: identifier("request"),
    idempotencyKey: identifier("idempotency"),
  });
}
