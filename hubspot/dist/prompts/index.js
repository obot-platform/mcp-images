// Map to store all registered prompt implementations
const promptMap = new Map();
/**
 * Register a prompt with the system
 */
export function registerPrompt(prompt) {
    const promptName = prompt.prompt.name;
    // Check for duplicate prompt names
    if (promptMap.has(promptName)) {
        console.error(`Prompt with name '${promptName}' already registered. Overwriting.`);
    }
    promptMap.set(promptName, prompt);
}
/**
 * Get all registered prompts
 */
export const getPrompts = () => Array.from(promptMap.values()).map(impl => impl.prompt);
/**
 * Get the prompt messages by name
 */
export const getPromptMessages = (name, args) => {
    const basePrompt = promptMap.get(name);
    if (!basePrompt) {
        console.error(`Unknown prompt: ${name}`);
        throw new Error(`Unknown prompt: ${name}`);
    }
    return basePrompt.getMessages(args);
};
