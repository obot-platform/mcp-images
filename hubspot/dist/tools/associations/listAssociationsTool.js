import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const AssociationsListSchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object to get associations from. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    objectId: z.string().describe('The ID of the HubSpot object to get associations from'),
    toObjectType: z
        .string()
        .describe(`The type of HubSpot object to get associations to. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    after: z
        .string()
        .optional()
        .describe('Paging cursor token for retrieving the next page of results'),
});
const ToolDefinition = {
    name: 'hubspot-list-associations',
    description: `
    🎯 Purpose:
      1. Retrieves existing relationships between a specific object and other objects of a particular type.
      2. For example, you can find all companies that a contact is associated with, all deals related to a company, or discover which customers have an open ticket.

    📦 Returns:
      1. Collection of associated object IDs and relationship metadata.
      2. Use hubspot-batch-read-objects to get more information about the associated objects.

    🧭 Usage Guidance:
      1. Use this tool when mapping relationships between different HubSpot objects to understand your data's connections.
      2. This tool is ideal when you already know a specific record's ID and need to discover its relationships with other object types.
      3. Prefer this over hubspot-search-objects tool when exploring established connections rather than filtering by properties or criteria.
  `,
    inputSchema: zodToJsonSchema(AssociationsListSchema),
    annotations: {
        title: 'List CRM Object Associations',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class AssociationsListTool extends BaseTool {
    client;
    constructor() {
        super(AssociationsListSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            // Build the API path
            let endpoint = `/crm/v4/objects/${args.objectType}/${args.objectId}/associations/${args.toObjectType}?limit=500`;
            // Add pagination parameter if provided
            if (args.after) {
                endpoint += `&after=${args.after}`;
            }
            // Make API request
            const response = await this.client.get(endpoint);
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
                        text: `Error retrieving HubSpot associations: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
