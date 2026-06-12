// Plan Tracker — OpenClaw plugin entry point
// This is a Python MCP server; the JS layer is minimal.

const PLUGIN = {
  id: "plan-tracker",
  name: "Plan Tracker",
  description:
    "MCP server for long-term plan tracking with milestones, check-ins, and scheduled reminders.",
};

// OpenClaw plugin contract
module.exports = {
  ...PLUGIN,

  /** Called when the plugin is loaded by OpenClaw Gateway. */
  register() {
    return PLUGIN;
  },

  /** Called when the plugin is activated (MCP server is started). */
  activate() {
    return PLUGIN;
  },
};
