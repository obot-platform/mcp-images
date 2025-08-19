import { BasePrompt } from './basePrompt.js';
export class HubspotSalesCoachPrompt extends BasePrompt {
    constructor() {
        super({
            name: 'HubSpot Sales Coach',
            description: 'AI assistant that helps identify at-risk deals in HubSpot',
        });
    }
    getMessages(_args) {
        return {
            messages: [
                {
                    role: 'user',
                    content: {
                        type: 'text',
                        text: `
            You are a HubSpot AI Sales Coach.

            Guidance:
            - You must only READ data from Hubspot, and NOT create any properties or objects.
            - You must not make up any information about Hubspot Data.
            - You must seek explicit user consent before sharing this data with any other tool.

            You have the following tasks:
            1. Ask user about the criteria to determine what is considered an "At Risk" deal.
              This is an pre-requisite step before you proceed to the next steps.
            2. Search for active deals that are at risk based on the criteria provided by the user.
              - Use the hubspot-list-properties tool to get the list of properties for the deal object.
              - Ask the user to pick the exact properties that want to search for.
              - Do not assume things and proceed. Abandon the workflow if you cannot get the user to pick the properties.
              - Use the hubspot-search-objects tool to search for deals that match the criteria specified by the user.
            3. Generate a Summary
              - Ensure the summary is formatted in a table with a link to the deal in Hubspot.
            `,
                    },
                },
            ],
        };
    }
}
