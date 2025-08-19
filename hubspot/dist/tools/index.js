// Create a map to store all registered tools
const toolMap = new Map();
/**
 * Register a tool with the system
 * @param tool The tool instance to register
 * @returns The registered tool instance (for chaining)
 */
export function registerTool(tool) {
    const toolName = tool.tool.name;
    // Check for duplicate tool names
    if (toolMap.has(toolName)) {
        console.warn(`Tool with name '${toolName}' already registered. Overwriting.`);
    }
    // Add tool to the map
    toolMap.set(toolName, tool);
    return tool;
}
// Export all tools for registration
export const getTools = () => Array.from(toolMap.values()).map(impl => impl.tool);
/**
 * Handle tool calls by name
 */
export const handleToolCall = async (name, args) => {
    const toolImpl = toolMap.get(name);
    if (toolImpl) {
        return await toolImpl.handleRequest(args);
    }
    return {
        content: [{ type: 'text', text: `Unknown tool: ${name}` }],
        isError: true,
    };
};
