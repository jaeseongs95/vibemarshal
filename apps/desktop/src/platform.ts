import type { PlatformAdapter, PlatformCapabilities, ProjectSummary } from "./contracts";

export class BrowserPlatformAdapter implements PlatformAdapter {
  getCapabilities(): PlatformCapabilities {
    const hasTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
    return {
      os: hasTauri ? "windows" : "browser-host",
      architecture: "x64",
      shell: hasTauri ? "tauri" : "browser",
      runtimeAvailable: true,
      filePickerAvailable: true,
      backgroundExecutionAvailable: true,
    };
  }

  async selectProjectDirectory(): Promise<ProjectSummary["location"] | null> {
    if (typeof window !== "undefined" && typeof window.prompt === "function") {
      const entered = window.prompt("전환할 로컬 프로젝트 디렉터리 절대 경로를 입력하세요:");
      if (entered && entered.trim()) {
        const trimmed = entered.trim();
        const parts = trimmed.split(/[\\/]/).filter(Boolean);
        const displayName = parts.at(-1) ?? "workspace";
        return {
          locationRef: `project-location:${displayName}`,
          displayName,
          displayPath: trimmed,
        };
      }
    }
    return null;
  }
}
