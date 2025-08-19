import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const OperatorEnum = z.enum([
    'EQ',
    'NEQ',
    'LT',
    'LTE',
    'GT',
    'GTE',
    'BETWEEN',
    'IN',
    'NOT_IN',
    'HAS_PROPERTY',
    'NOT_HAS_PROPERTY',
    'CONTAINS_TOKEN',
    'NOT_CONTAINS_TOKEN',
]);
const FilterSchema = z.object({
    propertyName: z.string().describe('The name of the property to filter by'),
    operator: OperatorEnum.describe('The operator to use for comparison'),
    value: z.string().describe('The value to compare against. Must be a string'), // Note from g-linville: in the NPM package, this is z.any()
    values: z
        .array(z.string()) // Note from g-linville: in the NPM package, this is array(z.any())
        .optional()
        .describe('Set of string values for multi-value operators like IN and NOT_IN.'),
    highValue: z
        .any()
        .optional()
        .describe('The upper bound value for range operators like BETWEEN. The lower bound is specified by the value attribute'),
});
const FilterGroupSchema = z.object({
    filters: z.array(FilterSchema).describe('Array of filters to apply (combined with AND).'),
});
const SortSchema = z.object({
    propertyName: z.string().describe('The name of the property to sort by'),
    direction: z.enum(['ASCENDING', 'DESCENDING']).describe('The sort direction'),
});
const ObjectSearchSchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object to search. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    query: z
        .string()
        .optional()
        .describe('Text to search across default searchable properties of the specified object type. Each object type has different searchable properties. For example: contacts (firstname, lastname, email, phone, company), companies (name, website, domain, phone), deals (dealname, pipeline, dealstage, description, dealtype), etc'),
    limit: z
        .number()
        .int()
        .min(1)
        .max(100)
        .default(10)
        .describe('The maximum number of results to display per page (max: 100).'),
    after: z
        .string()
        .optional()
        .describe('The paging cursor token of the last successfully read resource.'),
    properties: z
        .array(z.string())
        .optional()
        .describe('A list of the properties to be returned in the response.'),
    sorts: z
        .array(SortSchema)
        .optional()
        .describe('A list of sort criteria to apply to the results.'),
    filterGroups: z
        .array(FilterGroupSchema)
        .optional()
        .describe('Groups of filters to apply (combined with OR).'),
});
const ToolDefinition = {
    name: 'hubspot-search-objects',
    description: `
    🎯 Purpose:
      1. Performs advanced filtered searches across HubSpot object types using complex criteria.

    📋 Prerequisites:
      1. Use the hubspot-list-objects tool to sample existing objects for the object type.
      2. If hubspot-list-objects tool's response isn't helpful, use hubspot-list-properties tool.

    📦 Returns:
      1. Filtered collection matching specific criteria with pagination information.

    🧭 Usage Guidance:
      1. Preferred for targeted data retrieval when exact filtering criteria are known. Supports complex boolean logic through filter groups.
      2. Use hubspot-list-objects when filter criteria is not specified or clear or when a search fails.
      3. Use hubspot-batch-read-objects to retrieve specific objects by their IDs.
      4. Use hubspot-list-associations to get the associations between objects.

    🔍 Filtering Capabilities:
      1. Think of "filterGroups" as separate search conditions that you want to combine with OR logic (meaning ANY of them can match).
      2. If you want to find things that match ALL of several conditions (AND logic), put those conditions together in the same filters list.
      3. If you want to find things that match AT LEAST ONE of several conditions (OR logic), put each condition in a separate filterGroup.
      4. You can include a maximum of five filterGroups with up to 6 filters in each group, with a maximum of 18 filters in total.
  `,
    inputSchema: zodToJsonSchema(ObjectSearchSchema),
    annotations: {
        title: 'Search CRM Objects',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class ObjectSearchTool extends BaseTool {
    client;
    constructor() {
        super(ObjectSearchSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const { query, limit, after, properties, sorts, filterGroups } = args;
            const requestBody = {
                query,
                limit,
                after,
                properties: properties && properties.length > 0 ? properties : undefined,
                sorts: sorts && sorts.length > 0 ? sorts : undefined,
                filterGroups: filterGroups && filterGroups.length > 0 ? filterGroups : undefined,
            };
            const cleanRequestBody = Object.fromEntries(Object.entries(requestBody).filter(([_, value]) => value !== undefined));
            const response = await this.client.post(`/crm/v3/objects/${args.objectType}/search`, {
                body: cleanRequestBody,
            });
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
                        text: `Error searching HubSpot ${args.objectType}: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
