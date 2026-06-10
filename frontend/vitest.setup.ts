// Browser globals for Vitest (Pixi.js and WebSocket tests need these)

if (typeof globalThis.navigator === "undefined") {
  (globalThis as any).navigator = {
    userAgent: "vitest",
    gpu: undefined,
  };
}

if (typeof globalThis.CloseEvent === "undefined") {
  (globalThis as any).CloseEvent = class CloseEvent extends Event {
    code?: number;
    reason?: string;
    wasClean?: boolean;
    constructor(type: string, init?: { code?: number; reason?: string; wasClean?: boolean }) {
      super(type);
      this.code = init?.code ?? 0;
      this.reason = init?.reason ?? "";
      this.wasClean = init?.wasClean ?? true;
    }
  };
}

if (typeof globalThis.WebSocket === "undefined") {
  (globalThis as any).WebSocket = class WebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    readyState = 0;
    url = "";
    onopen: ((ev: Event) => void) | null = null;
    onmessage: ((ev: MessageEvent) => void) | null = null;
    onclose: ((ev: CloseEvent) => void) | null = null;
    onerror: ((ev: Event) => void) | null = null;
    constructor(url: string) {
      this.url = url;
    }
    send(_data: string) {}
    close() {}
  };
}
