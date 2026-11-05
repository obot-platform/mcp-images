import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHmac } from "node:crypto";
import { once } from "node:events";
import { createServer } from "node:http";
import { setTimeout } from "node:timers/promises";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

test("webhook tools preserve schemas, signing, validation, and error responses", { timeout: 20000 }, async (t) => {
  const requests = [];
  let responseStatus = 200;
  const webhook = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    requests.push({ headers: request.headers, body: Buffer.concat(chunks).toString() });
    response.writeHead(responseStatus).end(responseStatus === 200 ? "ok" : "unavailable");
  });
  webhook.listen(0, "127.0.0.1");
  await once(webhook, "listening");
  t.after(() => {
    webhook.closeAllConnections();
    webhook.close();
  });

  const reservation = createServer();
  reservation.listen(0, "127.0.0.1");
  await once(reservation, "listening");
  const port = reservation.address().port;
  await new Promise((resolve) => reservation.close(resolve));

  const secret = "test-only-secret";
  const child = spawn(process.execPath, ["server.js"], {
    cwd: new URL(".", import.meta.url),
    env: {
      ...process.env,
      PORT: String(port),
      WEBHOOK_URL: `http://127.0.0.1:${webhook.address().port}/hook`,
      WEBHOOK_SECRET: secret,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = "";
  child.stdout.on("data", (data) => { logs += data; });
  child.stderr.on("data", (data) => { logs += data; });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = once(child, "exit");
      child.kill();
      await exited;
    }
  });
  for (let attempt = 0; !logs.includes("Webhook MCP Server running"); attempt++) {
    assert.equal(child.exitCode, null, logs);
    assert.ok(attempt < 100, `Server did not start: ${logs}`);
    await setTimeout(50);
  }

  const client = new Client({ name: "webhook-regression-test", version: "1.0.0" });
  t.after(() => client.close());
  await client.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`)));
  const { tools } = await client.listTools();
  assert.deepEqual(tools.map((tool) => tool.name), ["fire-webhook"]);
  assert.equal(tools[0].inputSchema.properties.accept.type, "boolean");
  assert.equal(tools[0].inputSchema.properties.reason.type, "string");
  assert.equal(tools[0].outputSchema.properties.accept.type, "boolean");

  const message = { event: "test", values: [1, "two"] };
  const args = { accept: true, reason: "test", message };
  const accepted = await client.callTool({ name: "fire-webhook", arguments: args });
  assert.equal(accepted.isError, undefined);
  assert.deepEqual(accepted.structuredContent, { accept: true, reason: "Message accepted by webhook" });
  assert.deepEqual(JSON.parse(accepted.content[0].text), accepted.structuredContent);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body, JSON.stringify(message));
  assert.equal(requests[0].headers["x-obot-signature-256"],
    createHmac("sha256", secret).update(requests[0].body).digest("hex"));

  const invalid = await client.callTool({ name: "fire-webhook", arguments: { ...args, accept: "yes" } });
  assert.equal(invalid.isError, true);
  assert.equal(requests.length, 1);

  responseStatus = 503;
  const rejected = await client.callTool({ name: "fire-webhook", arguments: args });
  assert.deepEqual(rejected.structuredContent, { accept: false, reason: "Webhook returned error: 503 unavailable" });
  assert.deepEqual(JSON.parse(rejected.content[0].text), rejected.structuredContent);
  assert.equal(requests.length, 2);
});
