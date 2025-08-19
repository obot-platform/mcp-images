import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const ObjectReadInputSchema = z.object({
    id: z.string().describe('ID of the object to read'),
});
const BatchReadObjectsSchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object to read. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    inputs: z
        .array(ObjectReadInputSchema)
        .min(1)
        .max(100)
        .describe('Array of object IDs to read (maximum 100 per batch)'),
    properties: z
        .array(z.string())
        .optional()
        .describe('Optional list of property names to include in the results'),
    propertiesWithHistory: z
        .array(z.string())
        .optional()
        .describe('Optional list of property names to include with history'),
});
const ToolDefinition = {
    name: 'hubspot-batch-read-objects',
    description: `
    🎯 Purpose:
      1. Retrieves multiple HubSpot objects of the same object type by their IDs in a single batch operation.

    🧭 Usage Guidance:
      1. Use this tool to retrieve objects when the object IDs are known.
  `,
    inputSchema: zodToJsonSchema(BatchReadObjectsSchema),
    annotations: {
        title: 'Read Multiple CRM Objects',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
    },
};
export class BatchReadObjectsTool extends BaseTool {
    client;
    constructor() {
        super(BatchReadObjectsSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const requestBody = {
                inputs: args.inputs,
            };
            if (args.properties && args.properties.length > 0) {
                requestBody.properties = args.properties;
            }
            if (args.propertiesWithHistory && args.propertiesWithHistory.length > 0) {
                requestBody.propertiesWithHistory = args.propertiesWithHistory;
            }
            const response = await this.client.post(`/crm/v3/objects/${args.objectType}/batch/read`, {
                body: requestBody,
            });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            status: response.status,
                            results: response.results.map(result => {
                                const formattedResult = {
                                    id: result.id,
                                    properties: result.properties,
                                    createdAt: result.createdAt,
                                    updatedAt: result.updatedAt,
                                };
                                if (result.propertiesWithHistory) {
                                    formattedResult.propertiesWithHistory = result.propertiesWithHistory;
                                }
                                if (result.archived !== undefined) {
                                    formattedResult.archived = result.archived;
                                }
                                if (result.archivedAt) {
                                    formattedResult.archivedAt = result.archivedAt;
                                }
                                if (result.objectWriteTraceId) {
                                    formattedResult.objectWriteTraceId = result.objectWriteTraceId;
                                }
                                return formattedResult;
                            }),
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
                        text: `Error batch reading HubSpot objects: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
