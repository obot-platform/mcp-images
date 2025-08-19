import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const GetPropertySchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object the property belongs to. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    propertyName: z.string().describe('The name of the property to retrieve'),
});
const ToolDefinition = {
    name: 'hubspot-get-property',
    description: `
    🎯 Purpose:
      1. This tool retrieves detailed information about a specific property for a HubSpot object type.
      2. You can use this to get all metadata related to a property, including its type, options,
         and other configuration details.
  `,
    inputSchema: zodToJsonSchema(GetPropertySchema),
    annotations: {
        title: 'Get CRM Property Details',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
    },
};
export class GetPropertyTool extends BaseTool {
    client;
    constructor() {
        super(GetPropertySchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const response = await this.client.get(`/crm/v3/properties/${args.objectType}/${args.propertyName}`);
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
                        text: `Error retrieving HubSpot property ${args.propertyName} for ${args.objectType}: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
