import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
const GetEngagementSchema = z.object({
    engagementId: z.number().int().positive().describe('The ID of the engagement to retrieve'),
});
const ToolDefinition = {
    name: 'hubspot-get-engagement',
    description: `
    🎯 Purpose:
      1. Retrieves a HubSpot engagement by ID.
  `,
    inputSchema: zodToJsonSchema(GetEngagementSchema),
    annotations: {
        title: 'Get Engagement',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
    },
};
export class GetEngagementTool extends BaseTool {
    client;
    constructor() {
        super(GetEngagementSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const { engagementId } = args;
            const response = await this.client.get(`/engagements/v1/engagements/${engagementId}`);
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify(response, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return {
                content: [
                    {
                        type: 'text',
                        text: `Error retrieving HubSpot engagement: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
