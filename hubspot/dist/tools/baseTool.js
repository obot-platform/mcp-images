import { ToolSchema } from '@modelcontextprotocol/sdk/types.js';
// Type definition for input schema
const ToolInputSchema = ToolSchema.shape.inputSchema;
/**
 * Abstract class to implement MCP tools
 */
export class BaseTool {
    // The schema for validating tool arguments
    schema;
    // The tool definition for MCP
    tool;
    constructor(schema, tool) {
        this.schema = schema;
        this.tool = tool;
    }
    // Final handler method that performs validation before calling abstract process method
    async handleRequest(args) {
        try {
            // Validate args using schema
            const validatedArgs = this.schema.parse(args);
            // Call abstract process method with the validated args
            return await this.process(validatedArgs);
        }
        catch (error) {
            // Handle validation errors
            const errorMessage = error instanceof Error ? error.message : 'Unknown validation error';
            return {
                content: [{ type: 'text', text: `Error: ${errorMessage}` }],
                isError: true,
            };
        }
    }
}
// Export the tool input type for reuse in tool implementations
export { ToolInputSchema };
