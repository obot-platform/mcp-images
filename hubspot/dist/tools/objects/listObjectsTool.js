import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const ObjectListSchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object to list. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    limit: z
        .number()
        .int()
        .min(1)
        .max(500)
        .default(100)
        .describe('The maximum number of results to display per page (max: 500).'),
    after: z
        .string()
        .optional()
        .describe('The paging cursor token of the last successfully read resource.'),
    properties: z
        .array(z.string())
        .optional()
        .describe('A list of the properties to be returned in the response.'),
    associations: z
        .array(z.string())
        .optional()
        .describe(`A list of object types to retrieve associated IDs for (e.g., ${HUBSPOT_OBJECT_TYPES.join(', ')}).`),
    archived: z
        .boolean()
        .default(false)
        .describe('Whether to return only results that have been archived.'),
});
const ToolDefinition = {
    name: 'hubspot-list-objects',
    description: `
    🎯 Purpose:
      1. Retrieves a paginated list of objects of a specified type from HubSpot.

    📦 Returns:
      1. Collection of objects with their properties and metadata, plus pagination information.

    🧭 Usage Guidance:
      1. Use for initial data exploration to understand the data structure of a HubSpot object type.
      2. Helps list objects when the search criteria or filter criteria is not clear.
      3. Use hubspot-search-objects for targeted queries when the data structure is known.
      4. Use hubspot-batch-read-objects to retrieve specific objects by their IDs.
      5. Use hubspot-list-associations to list associations between objects.
  `,
    inputSchema: zodToJsonSchema(ObjectListSchema),
    annotations: {
        title: 'List CRM Objects',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class ObjectListTool extends BaseTool {
    client;
    constructor() {
        super(ObjectListSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const queryParams = new URLSearchParams();
            const paramMappings = {
                limit: args.limit?.toString(),
                after: args.after,
                properties: args.properties && args.properties.length > 0 ? args.properties.join(',') : undefined,
                associations: args.associations && args.associations.length > 0
                    ? args.associations.join(',')
                    : undefined,
                archived: args.archived?.toString() || 'false',
            };
            Object.entries(paramMappings).forEach(([key, value]) => {
                if (value !== undefined) {
                    queryParams.append(key, value);
                }
            });
            const response = await this.client.get(`/crm/v3/objects/${args.objectType}?${queryParams.toString()}`);
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            results: response.results.map(item => ({
                                id: item.id,
                                properties: item.properties,
                                createdAt: item.createdAt,
                                updatedAt: item.updatedAt,
                                archived: item.archived,
                                archivedAt: item.archivedAt,
                                associations: item.associations,
                            })),
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
                        text: `Error listing HubSpot ${args.objectType}: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
