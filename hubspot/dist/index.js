#!/usr/bin/env node
import 'dotenv/config';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, GetPromptRequestSchema, ListPromptsRequestSchema, ListToolsRequestSchema, } from '@modelcontextprotocol/sdk/types.js';
import { APP_NAME, APP_VERSION } from './utils/constants.js';
import { getPrompts, getPromptMessages } from './prompts/index.js';
import { getTools, handleToolCall } from './tools/index.js';
import './prompts/promptsRegistry.js';
import './tools/toolsRegistry.js';
const server = new Server({
    name: APP_NAME,
    version: APP_VERSION,
}, {
    capabilities: {
        tools: {},
        prompts: {},
        resources: {},
    },
});
// Handler for listing tools
server.setRequestHandler(ListToolsRequestSchema, async () => {
    return {
        tools: getTools(),
    };
});
// Handler for calling tools
server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    return handleToolCall(name, args);
});
// Handler for listing prompts
server.setRequestHandler(ListPromptsRequestSchema, async () => {
    return {
        prompts: getPrompts(),
    };
});
// Handler for getting specific prompt
server.setRequestHandler(GetPromptRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    return getPromptMessages(name, args);
});
async function main() {
    try {
        console.error('Starting HubSpot MCP Server...');
        // Connect to stdio transport
        const transport = new StdioServerTransport();
        await server.connect(transport);
        console.error('Server connected. Waiting for requests...');
    }
    catch (error) {
        console.error('Error starting server:', error);
        process.exit(1);
    }
}
// Handle graceful shutdown
process.on('SIGINT', async () => {
    console.error('Shutting down server...');
    await server.close();
    process.exit(0);
});
// Start the server
main();
