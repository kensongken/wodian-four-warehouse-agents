import {
  CaptureSessionSchema,
  type CaptureMode,
  type CaptureSession,
} from "@wearable/contracts";

export type RecorderState = "idle" | "recording" | "paused" | "stopped";

export type RecorderEvent =
  | { type: "led"; color: "green" | "yellow" | "red" }
  | { type: "prompt-tone" };

/** Local hardware-state simulation; it never sends or publishes capture data. */
export class Recorder {
  private captureCounter = 0;
  private activeSegment: Buffer[] = [];
  private _state: RecorderState = "idle";
  private _session: CaptureSession | null = null;
  readonly events: RecorderEvent[] = [];

  get state(): RecorderState {
    return this._state;
  }

  get session(): CaptureSession | null {
    return this._session;
  }

  start(mode: CaptureMode): CaptureSession {
    if (this._state === "recording" || this._state === "paused") {
      throw new Error("recorder-already-active");
    }

    this.captureCounter += 1;
    this._session = CaptureSessionSchema.parse({
      id: `local_capture_${this.captureCounter}`,
      startedAt: new Date().toISOString(),
      mode,
      privacyState: "creator",
      visitorConsent: "not-required",
      status: "recording",
      productRef: null,
    });
    this.activeSegment = [];
    this._state = "recording";
    this.events.push({ type: "led", color: "green" });
    return this._session;
  }

  appendSegment(bytes: Buffer): void {
    if (this._state === "paused") {
      throw new Error("privacy-paused");
    }
    if (this._state !== "recording") {
      throw new Error("recorder-not-recording");
    }

    this.activeSegment.push(Buffer.from(bytes));
  }

  pausePrivacy(): void {
    if (this._state !== "recording" || this._session === null) {
      throw new Error("recorder-not-recording");
    }

    this.activeSegment = [];
    this._state = "paused";
    this._session = CaptureSessionSchema.parse({
      ...this._session,
      privacyState: "paused",
      status: "stopped",
    });
    this.events.push({ type: "led", color: "red" });
  }

  resumeVisitor(consent: "confirmed" | "declined"): CaptureSession {
    if (this._state !== "paused" || this._session === null) {
      throw new Error("recorder-not-paused");
    }
    if (consent !== "confirmed") {
      throw new Error("visitor-consent-required");
    }

    this._state = "recording";
    this._session = CaptureSessionSchema.parse({
      ...this._session,
      privacyState: "visitor",
      visitorConsent: consent,
      status: "recording",
    });
    this.events.push({ type: "led", color: "yellow" }, { type: "prompt-tone" });
    return this._session;
  }

  stop(): CaptureSession {
    if (this._state !== "recording" || this._session === null) {
      throw new Error("recorder-not-recording");
    }

    this.activeSegment = [];
    this._state = "stopped";
    this._session = CaptureSessionSchema.parse({
      ...this._session,
      status: "stopped",
    });
    return this._session;
  }
}
