// Pop Quiz projector pane for Hermes Desktop (opt-in Desktop half of the popquiz plugin).
// It only frames the read-only projector page that the plugin serves on this machine
// (/screen, loopback by default) through the SDK's SandboxedFrame. No other surface is touched.
import { Button, Input, SandboxedFrame } from "@hermes/plugin-sdk";
import { useEffect, useState } from "react";
import { jsx, jsxs } from "react/jsx-runtime";

const STORAGE_KEY = "screenUrl";
const DEFAULT_URL = "http://127.0.0.1:8765/screen";

function isHttpUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function ProjectorPane({ storage }) {
  const [url, setUrl] = useState(DEFAULT_URL);
  const [draft, setDraft] = useState(DEFAULT_URL);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    Promise.resolve(storage.get(STORAGE_KEY))
      .then((saved) => {
        if (isHttpUrl(saved)) {
          setUrl(saved);
          setDraft(saved);
        }
      })
      .catch(() => {});
  }, [storage]);

  const show = (event) => {
    event.preventDefault();
    if (!isHttpUrl(draft)) return;
    setUrl(draft);
    setReload((n) => n + 1);
    Promise.resolve(storage.set(STORAGE_KEY, draft)).catch(() => {});
  };

  return jsxs("div", {
    style: { display: "flex", flexDirection: "column", height: "100%", gap: "6px", padding: "6px" },
    children: [
      jsxs("form", {
        onSubmit: show,
        style: { display: "flex", gap: "6px" },
        children: [
          jsx(Input, {
            value: draft,
            onChange: (event) => setDraft(event.target.value),
            "aria-label": "Pop Quiz screen URL",
            spellCheck: false,
          }),
          jsx(Button, { type: "submit", children: "Show" }),
        ],
      }),
      jsx(SandboxedFrame, {
        key: reload,
        src: url,
        title: "Pop Quiz projector",
        style: { flex: "1 1 auto", width: "100%", border: "0" },
      }),
    ],
  });
}

export default {
  id: "popquiz",
  name: "Pop Quiz",
  defaultEnabled: false,
  register(ctx) {
    ctx.register({
      id: "popquiz-screen",
      area: "panes",
      title: "Pop Quiz",
      data: { placement: "right", width: "560px" },
      render: () => jsx(ProjectorPane, { storage: ctx.storage }),
    });
  },
};
