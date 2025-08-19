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
const UpdatePropertySchema = z.object({
    objectType: z
        .string()
        .describe(`The type of HubSpot object the property belongs to. Valid values include: ${HUBSPOT_OBJECT_TYPES.join(', ')}. For custom objects, use the hubspot-get-schemas tool to get the objectType.`),
    propertyName: z.string().describe('The name of the property to update'),
    label: z
        .string()
        .optional()
        .describe('A human-readable property label that will be shown in HubSpot'),
    description: z
        .string()
        .optional()
        .describe('A description of the property that will be shown as help text'),
    groupName: z
        .string()
        .optional()
        .describe('The name of the property group the property belongs to'),
    type: z
        .enum(['string', 'number', 'date', 'datetime', 'enumeration', 'bool'])
        .optional()
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
        .optional()
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
    calculationFormula: z
        .string()
        .optional()
        .describe('A formula that is used to compute a calculated property'),
});
const ToolDefinition = {
    name: 'hubspot-update-property',
    description: `
    🛡️ Guardrails:
      1. Data Modification Warning: This tool modifies HubSpot data. Only use when the user has explicitly requested to update their CRM.

    🎯 Purpose:
      1. Updates existing custom properties for HubSpot object types, enabling data structure customization.

    🧭 Usage Guidance:
      1. Use hubspot-list-objects tool to sample existing objects for the object type.
      2. If hubspot-list-objects tool's response isn't helpful, use hubspot-list-properties tool.
  `,
    inputSchema: zodToJsonSchema(UpdatePropertySchema),
    annotations: {
        title: 'Update CRM Property',
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class UpdatePropertyTool extends BaseTool {
    client;
    constructor() {
        super(UpdatePropertySchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const { objectType, propertyName, ...updateData } = args;
            // Check if at least one field is provided for update
            if (Object.keys(updateData).length === 0) {
                throw new Error('At least one property field must be provided for update');
            }
            const response = await this.client.patch(`/crm/v3/properties/${objectType}/${propertyName}`, {
                body: updateData,
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
                        text: `Error updating HubSpot property ${args.propertyName} for ${args.objectType}: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
