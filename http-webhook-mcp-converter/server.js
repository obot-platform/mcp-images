import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import express from "express";
import { z } from "zod";
import { randomUUID, createHmac } from "node:crypto";

// Get configuration from environment variables
const WEBHOOK_URL = process.env.WEBHOOK_URL;
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET;

if (!WEBHOOK_URL) {
  console.error("Error: WEBHOOK_URL environment variable is required");
  process.exit(1);
}

const port = parseInt(process.env.PORT || "8099");

// Set up Express and HTTP transport
const app = express();
app.use(express.json({ limit: "50mb" }));

const transports = {};

app.post("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"];
  let transport;

  if (sessionId && transports[sessionId]) {
    // Reuse existing transport
    transport = transports[sessionId];
  } else if (!sessionId && isInitializeRequest(req.body)) {
    // New initialization request
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (sessionId) => {
        // Store the transport by session ID
        transports[sessionId] = transport;
      },
    });

    // Clean up transport when closed
    transport.onclose = () => {
      if (transport.sessionId) {
        delete transports[transport.sessionId];
      }
    };

    const server = new McpServer({
      name: "obot-webhook-server",
      version: "0.1.0",
    });

    // Register the webhook tool
    server.registerTool(
      "fire-webhook",
      {
        title: "Fire Webhook",
        description: "Send a message to the configured webhook URL",
        inputSchema: {
          accept: z.boolean(),
          reason: z.string(),
          message: z.any(),
        },
        outputSchema: {
          accept: z.boolean(),
          reason: z.string(),
        },
      },
      async ({ message }) => {
        const resp = {};
        try {
          const headers = {
            "Content-Type": "application/json",
          };

          // Serialize message to JSON string
          const messageBody = JSON.stringify(message);

          // Sign the payload if webhook secret is provided
          if (WEBHOOK_SECRET) {
            const hmac = createHmac("sha256", WEBHOOK_SECRET);
            hmac.update(messageBody);
            const signature = hmac.digest("hex");
            headers["X-Obot-Signature-256"] = signature;
          }

          // Send the request to the webhook
          const response = await fetch(WEBHOOK_URL, {
            method: "POST",
            headers,
            body: messageBody,
          });

          if (response.ok) {
            resp["accept"] = true;
            resp["reason"] = "Message accepted by webhook";
          } else {
            const errorBody = await response.text();
            resp["accept"] = false;
            resp["reason"] =
              `Webhook returned error: ${response.status} ${errorBody}`;
          }
        } catch (error) {
          resp["accept"] = false;
          resp["reason"] = `Failed to send to webhook: ${error.message}`;
        }
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(resp),
            },
          ],
          structuredContent: resp,
        };
      },
    );

    // Connect to the MCP server
    await server.connect(transport);
  } else {
    // Invalid request
    res.status(400).json({
      jsonrpc: "2.0",
      error: {
        code: -32000,
        message: "Bad Request: No valid session ID provided",
      },
      id: null,
    });
    return;
  }

  // Handle the request
  await transport.handleRequest(req, res, req.body);
});

// Reusable handler for GET and DELETE requests
const handleSessionRequest = async (req, res) => {
  const sessionId = req.headers["mcp-session-id"];
  if (!sessionId || !transports[sessionId]) {
    res.status(400).send("Invalid or missing session ID");
    return;
  }

  const transport = transports[sessionId];
  await transport.handleRequest(req, res);
};

// Handle GET requests for server-to-client notifications via SSE
app.get("/mcp", handleSessionRequest);

// Handle DELETE requests for session termination
app.delete("/mcp", handleSessionRequest);

app
  .listen(port, () => {
    console.log(`Webhook MCP Server running on http://localhost:${port}/mcp`);
    console.log(`Webhook URL: ${WEBHOOK_URL}`);
    console.log(
      `Webhook Secret: ${WEBHOOK_SECRET ? "configured" : "not configured"}`,
    );
  })
  .on("error", (error) => {
    console.error("Server error:", error);
    process.exit(1);
  });
