import { z } from 'zod';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { BaseTool } from '../baseTool.js';
import { HUBSPOT_ID_TO_OBJECT_TYPE } from '../../types/objectTypes.js';
// Define schema for the getHubspotLink tool
const PageTypeEnum = z
    .enum(['record', 'index'])
    .describe("The type of page to link to: 'record' for a specific object's page, 'index' for a list page");
const PageRequestSchema = z.object({
    pagetype: PageTypeEnum,
    objectTypeId: z
        .string()
        .describe("The HubSpot object type ID to link to (e.g., '0-1', '0-2' for contacts, companies, or '2-x' for custom objects)"),
    objectId: z
        .string()
        .optional()
        .describe("The specific object ID to link to (required for 'record' page types)"),
});
const GetHubspotLinkSchema = z.object({
    portalId: z.string().describe('The HubSpot portal/account ID'),
    uiDomain: z.string().describe("The HubSpot UI domain(e.g., 'app.hubspot.com')"),
    pageRequests: z.array(PageRequestSchema).describe('Array of page link requests to generate'),
});
const ToolDefinition = {
    name: 'hubspot-get-link',
    description: `
    🎯 Purpose:
      1. Generates HubSpot UI links for different pages based on object types and IDs.
      2. Supports both index pages (lists of objects) and record pages (specific object details).

    📋 Prerequisites:
      1. Use the hubspot-get-user-details tool to get the PortalId and UiDomain.

    🧭 Usage Guidance:
      1. Use to generate links to HubSpot UI pages when users need to reference specific HubSpot records.
      2. Validates that object type IDs exist in the HubSpot system.
  `,
    inputSchema: zodToJsonSchema(GetHubspotLinkSchema),
    annotations: {
        title: 'Get HubSpot Link',
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
    },
};
export class GetHubspotLinkTool extends BaseTool {
    constructor() {
        super(GetHubspotLinkSchema, ToolDefinition);
    }
    async process(args) {
        const { portalId, uiDomain, pageRequests } = args;
        const validationResult = this.validateRequests(pageRequests);
        if (validationResult.errors.length > 0) {
            return this.formatErrorResponse(validationResult);
        }
        const urlResults = this.generateUrls(portalId, uiDomain, pageRequests);
        return {
            content: [
                {
                    type: 'text',
                    text: JSON.stringify(urlResults, null, 2),
                },
            ],
        };
    }
    isValidObjectTypeId(objectTypeId) {
        if (Object.keys(HUBSPOT_ID_TO_OBJECT_TYPE).includes(objectTypeId)) {
            return true;
        }
        if (objectTypeId.startsWith('2-')) {
            return true;
        }
        return false;
    }
    validateRequests(pageRequests) {
        const errors = [];
        const invalidObjectTypeIds = [];
        for (const request of pageRequests) {
            const { pagetype, objectTypeId, objectId } = request;
            // Validate objectTypeId exists
            if (!this.isValidObjectTypeId(objectTypeId)) {
                invalidObjectTypeIds.push(objectTypeId);
                errors.push(`Invalid objectTypeId: ${objectTypeId}`);
                continue;
            }
            // For record pages, objectId is required
            if (pagetype === 'record' && !objectId) {
                errors.push(`objectId is required for record page with objectTypeId: ${objectTypeId}`);
            }
        }
        return { errors, invalidObjectTypeIds };
    }
    formatErrorResponse(validationResult) {
        const errorResponse = {
            errors: validationResult.errors,
        };
        // Add valid object type IDs only once if there were invalid IDs
        if (validationResult.invalidObjectTypeIds.length > 0) {
            errorResponse.validObjectTypeIds = Object.keys(HUBSPOT_ID_TO_OBJECT_TYPE);
            errorResponse.validObjectTypeIds.push('2-x (where x is your custom object ID)');
        }
        return {
            content: [
                {
                    type: 'text',
                    text: JSON.stringify(errorResponse, null, 2),
                },
            ],
            isError: true,
        };
    }
    generateUrls(portalId, uiDomain, pageRequests) {
        return pageRequests.map(request => {
            const { pagetype, objectTypeId, objectId } = request;
            let url = '';
            if (pagetype === 'index') {
                url = `https://${uiDomain}/contacts/${portalId}/objects/${objectTypeId}`;
            }
            else {
                url = `https://${uiDomain}/contacts/${portalId}/record/${objectTypeId}/${objectId}`;
            }
            return {
                pagetype,
                objectTypeId,
                objectId,
                url,
            };
        });
    }
}
