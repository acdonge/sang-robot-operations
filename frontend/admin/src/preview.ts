import { useEffect, useRef, useState } from "react";
import type { RobotSource } from "./types";

export type PreviewStatus = "idle" | "loading" | "live" | "error";

function appendBytes(left: Uint8Array, right: Uint8Array) {
  const combined = new Uint8Array(left.length + right.length);
  combined.set(left);
  combined.set(right, left.length);
  return combined;
}

function findMarker(bytes: Uint8Array, first: number, second: number, offset = 0) {
  for (let index = offset; index < bytes.length - 1; index += 1) {
    if (bytes[index] === first && bytes[index + 1] === second) return index;
  }
  return -1;
}

export function useMjpegPreview(identity: string) {
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [sourceKey, setSourceKey] = useState<string | null>(null);
  const [status, setStatus] = useState<PreviewStatus>("idle");
  const activeKeyRef = useRef<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const frameUrlRef = useRef<string | null>(null);

  function releaseFrame() {
    if (frameUrlRef.current) URL.revokeObjectURL(frameUrlRef.current);
    frameUrlRef.current = null;
    setFrameUrl(null);
  }

  function stop(nextStatus: PreviewStatus = "idle") {
    activeKeyRef.current = null;
    controllerRef.current?.abort();
    controllerRef.current = null;
    releaseFrame();
    setSourceKey(null);
    setStatus(nextStatus);
  }

  useEffect(() => {
    stop();
  }, [identity]);

  useEffect(() => () => {
    activeKeyRef.current = null;
    controllerRef.current?.abort();
    if (frameUrlRef.current) URL.revokeObjectURL(frameUrlRef.current);
  }, []);

  async function start(source: RobotSource) {
    if (!source.preview_url) return;
    stop();
    const controller = new AbortController();
    const streamKey = `${identity}:${source.key}`;
    activeKeyRef.current = streamKey;
    controllerRef.current = controller;
    setSourceKey(source.key);
    setStatus("loading");
    let buffer = new Uint8Array(0);

    try {
      const separator = source.preview_url.includes("?") ? "&" : "?";
      const response = await fetch(`${source.preview_url}${separator}session=${Date.now()}`, {
        cache: "no-store",
        headers: { Accept: "multipart/x-mixed-replace" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Preview HTTP ${response.status}`);
      if (!response.body) throw new Error("Preview stream body unavailable");
      const reader = response.body.getReader();

      while (activeKeyRef.current === streamKey && !controller.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) throw new Error("Preview stream ended");
        buffer = appendBytes(buffer, value);
        while (buffer.length) {
          const startIndex = findMarker(buffer, 0xff, 0xd8);
          if (startIndex < 0) {
            buffer = buffer.slice(-1);
            break;
          }
          const endIndex = findMarker(buffer, 0xff, 0xd9, startIndex + 2);
          if (endIndex < 0) {
            if (startIndex > 0) buffer = buffer.slice(startIndex);
            break;
          }
          if (activeKeyRef.current !== streamKey) break;
          const nextUrl = URL.createObjectURL(new Blob(
            [buffer.slice(startIndex, endIndex + 2)],
            { type: "image/jpeg" },
          ));
          if (frameUrlRef.current) URL.revokeObjectURL(frameUrlRef.current);
          frameUrlRef.current = nextUrl;
          setFrameUrl(nextUrl);
          setStatus("live");
          buffer = buffer.slice(endIndex + 2);
        }
      }
      await reader.cancel();
    } catch {
      if (controller.signal.aborted || activeKeyRef.current !== streamKey) return;
      activeKeyRef.current = null;
      controllerRef.current = null;
      releaseFrame();
      setStatus("error");
    }
  }

  return {
    active: status === "loading" || status === "live",
    frameUrl,
    sourceKey,
    start,
    status,
    stop,
  };
}
