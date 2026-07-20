// Plan Tracker — OpenClaw plugin entry point
// Provides a privacy-safe CLI subcommand for notification delivery.

const PLUGIN = {
  id: "plan-tracker",
  name: "Plan Tracker",
  description:
    "MCP server for long-term plan tracking with milestones, check-ins, and scheduled reminders.",
};

const MAX_STDIN_BYTES = 65536;

function validatePayload(data) {
  if (!data || typeof data !== "object")
    throw new Error("payload must be a JSON object");
  if (!data.channel || typeof data.channel !== "string")
    throw new Error("payload.channel is required (string)");
  if (!data.target || typeof data.target !== "string")
    throw new Error("payload.target is required (string)");
  if (!data.message || typeof data.message !== "string")
    throw new Error("payload.message is required (string)");
  if (data.channel.length > 64 || data.target.length > 1024)
    throw new Error("payload channel or target is too long");
  if (/[\u0000-\u001f\u007f]/u.test(data.channel + data.target))
    throw new Error("payload channel or target contains control characters");
  if (Buffer.byteLength(data.message, "utf8") > 32768)
    throw new Error("payload.message too long (max 32 KiB)");
  if (
    data.idempotencyKey !== undefined &&
    (typeof data.idempotencyKey !== "string" || data.idempotencyKey.length > 128)
  )
    throw new Error("payload.idempotencyKey must be a string up to 128 characters");
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    let settled = false;
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      if (settled) return;
      total += Buffer.byteLength(chunk, "utf8");
      if (total > MAX_STDIN_BYTES) {
        settled = true;
        process.stdin.pause();
        reject(new Error("stdin payload exceeds 64 KiB limit"));
        return;
      }
      chunks.push(chunk);
    });
    process.stdin.on("end", () => {
      if (!settled) {
        settled = true;
        resolve(chunks.join(""));
      }
    });
    process.stdin.on("error", (error) => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    });
  });
}

async function deliverNotification() {
  let raw;
  try { raw = await readStdin(); } catch (e) {
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
    api.registerCli(
      // Registrar function — receives Commander context
      (ctx) => {
        ctx.program
          .command("plan-tracker-deliver")
          .description("Deliver a plan-tracker notification via stdin (privacy-safe)")
          .action(async () => {
            await deliverNotification();
          });
      },
      // Metadata for lazy/eager CLI registration
      {
        commands: ["plan-tracker-deliver"],
        descriptors: [
          {
            name: "plan-tracker-deliver",
            description:
              "Deliver a plan-tracker notification via stdin (privacy-safe)",
          },
        ],
      },
    );

    return PLUGIN;
  },

  activate() {
    return PLUGIN;
  },
};
