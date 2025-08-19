import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
const GetWorkflowSchema = z.object({
    flowId: z.string().describe('The ID of the workflow to retrieve'),
});
const ToolDefinition = {
    name: 'hubspot-get-workflow',
    description: `
    🎯 Purpose:
      1. This tool retrieves detailed information about a specific workflow from the HubSpot account.

    🧭 Usage Guidance:
      1. Use the "flowId" parameter to specify which workflow to retrieve.
      2. This endpoint returns complete workflow information including actions, enrollment criteria, and scheduling.
      3. Use the hubspot-list-workflows tool first to identify the workflow ID you need.
  `,
    inputSchema: zodToJsonSchema(GetWorkflowSchema),
    annotations: {
        title: 'Get HubSpot Workflow Details',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
    },
};
export class GetWorkflowTool extends BaseTool {
    client;
    constructor() {
        super(GetWorkflowSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const response = await this.client.get(`/automation/v4/flows/${args.flowId}`);
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
                        text: `Error retrieving HubSpot workflow (ID: ${args.flowId}): ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
