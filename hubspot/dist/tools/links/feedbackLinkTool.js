import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
// Define schema for the feedback tool
const FeedbackLinkSchema = z.object({});
const ToolDefinition = {
    name: 'hubspot-generate-feedback-link',
    description: `
    🎯 Purpose:
      1. Use this tool when the user wants to submit feedback about HubSpot MCP tool.
      2. Use this tool proactively when the other HubSpot MCP tools are unable to solve the user's tasks effectively.
      3. Use this tool when you sense dissatisfaction from the user using HubSpot MCP tools.
      4. Feedback will help us improve the HubSpot MCP tools in future iterations.
  `,
    inputSchema: zodToJsonSchema(FeedbackLinkSchema),
    annotations: {
        title: 'Generate Feedback Link',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
    },
};
export class FeedbackLinkTool extends BaseTool {
    constructor() {
        super(FeedbackLinkSchema, ToolDefinition);
    }
    async process(args) {
        const feedbackUrl = 'https://developers.hubspot.com/mcp';
        const message = `Share Feedback link with the user and ask the user to provide feedback: ${feedbackUrl}`;
        return {
            content: [
                {
                    type: 'text',
                    text: message,
                },
            ],
        };
    }
}
