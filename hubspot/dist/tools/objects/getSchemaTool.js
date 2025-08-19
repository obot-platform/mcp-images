import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
const SchemaInfoSchema = z.object({});
const ToolDefinition = {
    name: 'hubspot-get-schemas',
    description: `
    🎯 Purpose:
      1. Retrieves all custom object schemas defined in the HubSpot account.

    🧭 Usage Guidance:
      1. Before working with custom objects to understand available object types,
         their properties, and associations.

    📦 Returns:
      1. Provides the objectTypeId and objectType for each schema.
      2. These attributes should be used for this object type instead of "custom" in subsequent requests.
  `,
    inputSchema: zodToJsonSchema(SchemaInfoSchema),
    annotations: {
        title: 'Get Object Schemas',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
    },
};
export class GetSchemasTool extends BaseTool {
    client;
    constructor() {
        super(SchemaInfoSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(_args) {
        try {
            const schemas = await this.client.get('/crm-object-schemas/v3/schemas');
            const simplifiedResults = schemas.results.map((schema) => ({
                objectTypeId: schema.objectTypeId,
                objectType: schema.fullyQualifiedName.split('_')[1],
                name: schema.name,
                labels: schema.labels,
            }));
            return {
                content: [
                    {
                        type: 'text',
                        text: 'Custom object schemas found. Note: These attributes should be used instead of "custom" in subsequent requests:',
                    },
                    {
                        type: 'text',
                        text: JSON.stringify({ results: simplifiedResults }, null, 2),
                    },
                ],
            };
        }
        catch (error) {
            return {
                content: [
                    {
                        type: 'text',
                        text: `Error retrieving schemas: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
