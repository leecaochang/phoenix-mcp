export interface InjectController {
  build: string;
  dispose: () => void;
}

function buildParts(build: string): number[] {
  return build.split(".").map((part) => Number.parseInt(part, 10) || 0);
}

export function compareBuilds(left: string, right: string): number {
  const a = buildParts(left);
  const b = buildParts(right);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const delta = (a[i] ?? 0) - (b[i] ?? 0);
    if (delta !== 0) return delta;
  }
  return 0;
}

/** Let the newest injected module own a page and dispose the older build. */
export function claimInjectController(
  host: Record<string, unknown>,
  key: string,
  controller: InjectController,
): boolean {
  const current = host[key] as Partial<InjectController> | undefined;
  if (
    current
    && typeof current.build === "string"
    && typeof current.dispose === "function"
  ) {
    if (compareBuilds(current.build, controller.build) >= 0) return false;
    current.dispose();
  }
  host[key] = controller;
  return true;
}
