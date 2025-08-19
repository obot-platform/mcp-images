import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
const WorkflowsListSchema = z.object({
    limit: z
        .number()
        .min(1)
        .max(100)
        .optional()
        .default(20)
        .describe('The maximum number of workflows to return per page (1-100).'),
    after: z
        .string()
        .optional()
        .describe('Cursor token to fetch the next page of results. Use the paging.next.after value from the previous response.'),
});
const ToolDefinition = {
    name: 'hubspot-list-workflows',
    description: `
    🎯 Purpose:
      1. This tool retrieves a paginated list of workflows from the HubSpot account.

    🧭 Usage Guidance:
      1. Use the "limit" parameter to control the number of results returned per page.
      2. For pagination, use the "after" parameter with the value from the previous response's paging.next.after.
      3. This endpoint returns essential workflow information including ID, name, type, and status.
  `,
    inputSchema: zodToJsonSchema(WorkflowsListSchema),
    annotations: {
        title: 'List HubSpot Workflows',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
    },
};
export class WorkflowsListTool extends BaseTool {
    client;
    constructor() {
        super(WorkflowsListSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const params = {};
            if (args.limit) {
                params.limit = args.limit;
            }
            if (args.after) {
                params.after = args.after;
            }
            const response = await this.client.get('/automation/v4/flows', {
                params,
            });
            const filteredResults = response.results;
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            results: filteredResults,
                            paging: response.paging,
                        }, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return {
                content: [
                    {
                        type: 'text',
                        text: `Error listing HubSpot workflows: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
