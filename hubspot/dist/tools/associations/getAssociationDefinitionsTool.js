import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const AssociationSchemaDefinitionSchema = z.object({
    fromObjectType: z
        .string()
        .describe(`The type of HubSpot object to get association from. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    toObjectType: z
        .string()
        .describe(`The type of HubSpot object to get association to. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
});
const ToolDefinition = {
    name: 'hubspot-get-association-definitions',
    description: `
    🎯 Purpose:
      1. Retrieves valid association types between specific HubSpot object types.

    📦 Returns:
      1. Array of valid association definitions with type IDs, labels, and categories.

    🧭 Usage Guidance:
      1. Always use before creating associations to ensure valid relationship types or to help troubleshoot association creation errors.
  `,
    inputSchema: zodToJsonSchema(AssociationSchemaDefinitionSchema),
    annotations: {
        title: 'Get CRM Association Types',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
    },
};
export class AssociationSchemaDefinitionTool extends BaseTool {
    client;
    constructor() {
        super(AssociationSchemaDefinitionSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const response = await this.client.get(`/crm/v4/associations/${args.fromObjectType}/${args.toObjectType}/labels`);
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
                        text: `Error retrieving HubSpot association schema definitions: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
