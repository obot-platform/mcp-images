import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import HubSpotClient from '../../utils/client.js';
// Define the engagement types we support
const ENGAGEMENT_TYPES = ['NOTE', 'TASK'];
const NoteMetadataSchema = z.object({
    body: z.string().describe('The content of the note'),
});
const TaskMetadataSchema = z.object({
    body: z.string().describe('The body/description of the task'),
    subject: z.string().describe('The title/subject of the task'),
    status: z.enum(['NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'WAITING']).default('NOT_STARTED'),
    forObjectType: z.enum(['CONTACT', 'COMPANY', 'DEAL', 'TICKET']).default('CONTACT'),
});
export const AssociationsSchema = z.object({
    contactIds: z.array(z.number().int()).optional().default([]),
    companyIds: z.array(z.number().int()).optional().default([]),
    dealIds: z.array(z.number().int()).optional().default([]),
    ownerIds: z.array(z.number().int()).optional().default([]),
    ticketIds: z.array(z.number().int()).optional().default([]),
});
// Map engagement types to their metadata schemas
const metadataSchemas = {
    NOTE: NoteMetadataSchema,
    TASK: TaskMetadataSchema,
};
const CreateEngagementSchema = z
    .object({
    type: z.enum(ENGAGEMENT_TYPES).describe('The type of engagement to create (NOTE or TASK)'),
    ownerId: z.number().int().positive().describe('The ID of the owner of this engagement'),
    timestamp: z
        .number()
        .int()
        .optional()
        .describe('Timestamp for the engagement (milliseconds since epoch). Defaults to current time if not provided.'),
    associations: AssociationsSchema.describe('Associated records for this engagement'),
    metadata: z.object({}).passthrough().describe('Metadata specific to the engagement type'),
})
    .superRefine((data, ctx) => {
    const schema = metadataSchemas[data.type];
    if (!schema) {
        ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: `Unsupported engagement type: ${data.type}`,
            path: ['type'],
        });
        return;
    }
    const result = schema.safeParse(data.metadata);
    if (!result.success) {
        result.error.issues.forEach(issue => {
            ctx.addIssue({
                ...issue,
                path: ['metadata', ...(issue.path || [])],
            });
        });
    }
});
const ToolDefinition = {
    name: 'hubspot-create-engagement',
    description: `
    🛡️ Guardrails:
      1. Data Modification Warning: This tool modifies HubSpot data. Only use when the user has explicitly requested to update their CRM.

    🎯 Purpose:
      1. Creates a HubSpot engagement (Note or Task) associated with contacts, companies, deals, or tickets.
      2. This endpoint is useful for keeping your CRM records up-to-date on any interactions that take place outside of HubSpot.
      3. Activity reporting in the CRM also feeds off of this data.

    📋 Prerequisites:
      1. Use the hubspot-get-user-details tool to get the OwnerId and UserId.

    🧭 Usage Guidance:
      1. Use NOTE type for adding notes to records
      2. Use TASK type for creating tasks with subject, status, and assignment
      3. Both require relevant associations to connect them to CRM records
      4. Other types of engagements (EMAIL, CALL, MEETING) are NOT supported yet.
      5. HubSpot notes and task descriptions support HTML formatting. However headings (<h1>, <h2>, etc.) look ugly in the CRM. So use them sparingly.
  `,
    inputSchema: zodToJsonSchema(CreateEngagementSchema),
    annotations: {
        title: 'Create Engagement',
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
    },
};
export class CreateEngagementTool extends BaseTool {
    client;
    constructor() {
        super(CreateEngagementSchema, ToolDefinition);
        this.client = new HubSpotClient();
    }
    async process(args) {
        try {
            const { type, ownerId, timestamp, associations, metadata } = args;
            const engagementTimestamp = timestamp || Date.now();
            const requestBody = {
                engagement: {
                    active: true,
                    ownerId,
                    type,
                    timestamp: engagementTimestamp,
                },
                associations,
                metadata,
            };
            const response = await this.client.post('/engagements/v1/engagements', {
                body: requestBody,
            });
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            status: 'success',
                            engagement: response,
                            message: `Successfully created ${type.toLowerCase()} engagement`,
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
                        text: `Error creating HubSpot engagement: ${error instanceof Error ? error.message : String(error)}`,
                    },
                ],
                isError: true,
            };
        }
    }
}
