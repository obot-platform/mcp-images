import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
import { HUBSPOT_OBJECT_TYPES } from '../../types/objectTypes.js';
const PropertyOptionSchema = z.object({
    label: z.string().describe('The human-readable label for the option'),
    value: z
        .string()
        .describe('The internal value for the option, which must be used when setting the property value'),
    description: z.string().optional().describe('A description of what the option represents'),
    displayOrder: z
        .number()
        .int()
        .optional()
        .describe('The order for displaying the option (lower numbers displayed first)'),
    hidden: z.boolean().optional().describe('Whether the option should be hidden in HubSpot'),
});
const CreatePropertySchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object to create the property for. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    name: z
        .string()
        .describe('The internal property name, which must be used when referencing the property via the API'),
    label: z.string().describe('A human-readable property label that will be shown in HubSpot'),
    description: z
        .string()
        .optional()
        .describe('A description of the property that will be shown as help text'),
    groupName: z.string().describe('The name of the property group the property belongs to'),
    type: z
        .enum(['string', 'number', 'date', 'datetime', 'enumeration', 'bool'])
        .default('string')
        .describe('The data type of the property'),
    fieldType: z
        .enum([
        'text',
        'textarea',
        'date',
        'file',
        'number',
        'select',
        'radio',
        'checkbox',
        'booleancheckbox',
        'calculation',
    ])
        .default('text')
        .describe('Controls how the property appears in HubSpot'),
    options: z
        .array(PropertyOptionSchema)
        .optional()
        .describe('A list of valid options for enumeration properties'),
    formField: z.boolean().optional().describe('Whether the property can be used in forms'),
    hidden: z.boolean().optional().describe('Whether the property should be hidden in HubSpot'),
    displayOrder: z
        .number()
        .int()
        .optional()
        .describe('The order for displaying the property (lower numbers displayed first)'),
    hasUniqueValue: z.boolean().optional().describe("Whether the property's value must be unique"),
    calculationFormula: z
        .string()
        .optional()
        .describe('A formula that is used to compute a calculated property'),
    externalOptions: z
        .boolean()
        .optional()
        .describe('Only for enumeration type properties. Should be set to true in conjunction with a referencedObjectType'),
});
const ToolDefinition = {
    name: 'hubspot-create-property',
    description: `
    🛡️ Guardrails:
      1. Data Modification Warning: This tool modifies HubSpot data. Only use when the user has explicitly requested to update their CRM.

    🎯 Purpose:
      1. Creates new custom properties for HubSpot object types, enabling data structure customization.

    📋 Prerequisites:
      1. Use the hubspot-get-user-details tool to get the OwnerId and UserId if you don't have that already.
      2. Use the hubspot-list-objects tool to sample existing objects for the object type.
      3. If hubspot-list-objects tool's response isn't helpful, use hubspot-list-properties tool.

    🧭 Usage Guidance:
      1. Use this tool when you need to create a new custom property for a HubSpot object type.
      2. Makes sure that the user is looking to create a new property, and not create an object of a specific object type.
      3. Use list-properties to get a list of all properties for a given object type to be sure that the property does not already exist.
      4. Use list-properties to to understand the data structure of object properties first.
  `,
    inputSchema: zodToJsonSchema(CreatePropertySchema),
    annotations: {
        title: 'Create CRM Property',
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class CreatePropertyTool extends BaseTool {
    client;
    constructor() {
        super(CreatePropertySchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const propertyData = {
                ...args,
                // Ensure name follows HubSpot naming convention (lowercase, no spaces)
                name: args.name.toLowerCase().replace(/\s+/g, '_'),
            };
            const { objectType, ...dataWithoutObjectType } = propertyData;
            const cleanPropertyData = Object.fromEntries(Object.entries(dataWithoutObjectType).filter(([_, value]) => value !== undefined));
            const response = await this.client.post(`/crm/v3/properties/${args.objectType}`, {
                body: cleanPropertyData,
            });
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
                        text: `Error creating HubSpot property for ${args.objectType}: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
