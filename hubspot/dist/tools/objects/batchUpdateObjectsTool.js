import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const PropertiesSchema = z.record(z.string(), z.string());
const ObjectUpdateInputSchema = z.object({
    id: z.string().describe('ID of the object to update'),
    properties: PropertiesSchema.describe('Object properties as key-value pairs'),
    idProperty: z.string().optional().describe('Optional unique property name to use as the ID'),
    objectWriteTraceId: z.string().optional().describe('Optional trace ID for debugging purposes'),
});
const BatchUpdateObjectsSchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object to update. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    inputs: z
        .array(ObjectUpdateInputSchema)
        .min(1)
        .max(100)
        .describe('Array of objects to update (maximum 100 per batch)'),
});
const ToolDefinition = {
    name: 'hubspot-batch-update-objects',
    description: `
    🛡️ Guardrails:
      1. Data Modification Warning: This tool modifies HubSpot data. Only use when the user has explicitly requested to update their CRM.

    🎯 Purpose:
      1. Updates multiple existing HubSpot objects of the same objectType in a single API call.
      2. Use this tool when the user wants to update one or more existing CRM objects.
      3. If you are unsure about the property type to update, identify existing properties of the object and ask the user.

    📋 Prerequisites:
      1. Use the hubspot-get-user-details tool to get the OwnerId and UserId if you don't have that already.
      2. Use the hubspot-list-objects tool to sample existing objects for the object type.
      3. If hubspot-list-objects tool's response isn't helpful, use hubspot-list-properties tool.
  `,
    inputSchema: zodToJsonSchema(BatchUpdateObjectsSchema),
    annotations: {
        title: 'Update Multiple CRM Objects',
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class BatchUpdateObjectsTool extends BaseTool {
    client;
    constructor() {
        super(BatchUpdateObjectsSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const response = await this.client.post(`/crm/v3/objects/${args.objectType}/batch/update`, {
                body: {
                    inputs: args.inputs,
                },
            });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            status: response.status,
                            results: response.results.map(result => ({
                                id: result.id,
                                properties: result.properties,
                                createdAt: result.createdAt,
                                updatedAt: result.updatedAt,
                                archived: result.archived,
                                archivedAt: result.archivedAt,
                            })),
                            requestedAt: response.requestedAt,
                            startedAt: response.startedAt,
                            completedAt: response.completedAt,
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
                        text: `Error batch updating HubSpot objects: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
