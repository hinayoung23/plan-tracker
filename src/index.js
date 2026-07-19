// Plan Tracker — OpenClaw plugin entry point
// Provides a privacy-safe CLI subcommand for notification delivery.

const PLUGIN = {
  id: "plan-tracker",
  name: "Plan Tracker",
  description:
    "MCP server for long-term plan tracking with milestones, check-ins, and scheduled reminders.",
};

const MAX_STDIN_BYTES = 65536; // 64 KiB

function validatePayload(data) {
  if (!data || typeof data !== "object") {
    throw new Error("payload must be a JSON object");
  }
  if (!data.channel || typeof data.channel !== "string") {
    throw new Error("payload.channel is required (string)");
  }
  if (!data.target || typeof data.target !== "string") {
    throw new Error("payload.target is required (string)");
  }
  if (!data.message || typeof data.message !== "string") {
    throw new Error("payload.message is required (string)");
  }
  if (data.message.length > 32768) {
    throw new Error("payload.message too long (max 32 KiB)");
  }
  if (data.idempotencyKey && typeof data.idempotencyKey !== "string") {
    throw new Error("payload.idempotencyKey must be a string");
  }
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      total += Buffer.byteLength(chunk, "utf8");
      if (total > MAX_STDIN_BYTES) {
        reject(new Error("stdin payload exceeds 64 KiB limit"));
        return;
      }
      chunks.push(chunk);
    });
    process.stdin.on("end", () => resolve(chunks.join("")));
    process.stdin.on("error", reject);
  });
}

// ── CLI command handler ──────────────────────────────────────────
async function deliverCommand() {
  let raw;
  try {
    raw = await readStdin();
  } catch (e) {
    console.error(JSON.stringify({ ok: false, error: e.message }));
    process.exit(1);
  }

  let payload;
  try {
    payload = JSON.parse(raw);
    validatePayload(payload);
  } catch (e) {
    console.error(JSON.stringify({ ok: false, error: e.message }));
    process.exit(1);
  }

  const gw = await import("openclaw/plugin-sdk/gateway-runtime");
  try {
    await gw.callGatewayFromCli(
      "send",
      { json: true, timeout: "15000" },
      {
        channel: payload.channel,
        to: payload.target,
        message: payload.message,
        idempotencyKey: payload.idempotencyKey || undefined,
      },
      { progress: false, scopes: ["operator.write"] },
    );
    console.log(JSON.stringify({ ok: true }));
  } catch (e) {
    console.error(JSON.stringify({ ok: false, error: "delivery failed" }));
    process.exit(1);
  }
}

// OpenClaw plugin contract
module.exports = {
  ...PLUGIN,

  register(api) {
    // Registrar function: receives a register() callback.
    // OpenClaw 2026.7.1 requires this pattern with explicit commands metadata.
    api.registerCli((register) => {
      register({
        name: "plan-tracker-deliver",
        description: "Deliver a plan-tracker notification via stdin (privacy-safe)",
        commands: {
          default: {
            description: "Read JSON payload from stdin and deliver via Gateway send()",
            run: deliverCommand,
          },
        },
      });
    });

    return PLUGIN;
  },

  activate() {
    return PLUGIN;
  },
};
