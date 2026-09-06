import type { PlatformAdapter, PlatformCapabilities, ProjectSummary } from "./contracts";

export class BrowserPlatformAdapter implements PlatformAdapter {
  getCapabilities(): PlatformCapabilities {
    return {
      os: "browser-host",
      architecture: "unknown",
      shell: "browser",
      runtimeAvailable: false,
      filePickerAvailable: false,
      backgroundExecutionAvailable: false,
    };
  }

  async selectProjectDirectory(): Promise<ProjectSummary["location"] | null> {
    return null;
  }
}
