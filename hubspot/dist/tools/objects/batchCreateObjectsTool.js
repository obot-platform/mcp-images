import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const AssociationTypeSchema = z.object({
    associationCategory: z.enum(['HUBSPOT_DEFINED', 'USER_DEFINED', 'INTEGRATOR_DEFINED']),
    associationTypeId: z.number().int().positive(),
});
const AssociationSchema = z.object({
    types: z.array(AssociationTypeSchema).min(1),
    to: z.object({
        id: z.string().describe('ID of the object to associate with'),
    }),
});
const PropertiesSchema = z.record(z.string(), z.string());
const ObjectInputSchema = z.object({
    properties: PropertiesSchema.describe('Object properties as key-value pairs'),
    associations: z
        .array(AssociationSchema)
        .optional()
        .describe('Optional list of associations to create with this object'),
    objectWriteTraceId: z.string().optional().describe('Optional trace ID for debugging purposes'),
});
const BatchCreateObjectsSchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object to create. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    inputs: z
        .array(ObjectInputSchema)
        .min(1)
        .max(100)
        .describe('Array of objects to create (maximum 100 per batch)'),
});
const ToolDefinition = {
    name: 'hubspot-batch-create-objects',
    description: `
    🛡️ Guardrails:
      1. Data Modification Warning: This tool modifies HubSpot data. Only use when the user has explicitly requested to update their CRM.

    🎯 Purpose:
      1. Creates multiple HubSpot objects of the same objectType in a single API call, optimizing for bulk operations.

    📋 Prerequisites:
      1. Use the hubspot-get-user-details tool to get the OwnerId and UserId if you don't have that already.
      2. Use the hubspot-list-objects tool to sample existing objects for the object type.
      3. Use the hubspot-get-association-definitions tool to identify valid association types before creating associations.
  `,
    inputSchema: zodToJsonSchema(BatchCreateObjectsSchema),
    annotations: {
        title: 'Create CRM Objects',
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class BatchCreateObjectsTool extends BaseTool {
    client;
    constructor() {
        super(BatchCreateObjectsSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const response = await this.client.post(`/crm/v3/objects/${args.objectType}/batch/create`, {
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
                        text: `Error batch creating HubSpot objects. : ${error instanceof Error ? error.message : String(error)}
            `,
                    },
                ],
                isError: true,
            };
        }
    }
}
