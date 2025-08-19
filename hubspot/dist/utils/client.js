import { URLSearchParams } from 'url';
import { APP_NAME, APP_VERSION } from './constants.js';
export class HubSpotClient {
    baseUrl;
    accessToken;
    constructor() {
        this.baseUrl = process.env.BASE_URL_OVERRIDE || 'https://api.hubspot.com';
        // HUBSPOT_ACCESS_TOKEN is kept for backwards compatibility.
        this.accessToken =
            process.env.PRIVATE_APP_ACCESS_TOKEN || process.env.HUBSPOT_ACCESS_TOKEN || '';
        if (!this.accessToken) {
            throw new Error('HubSpot access token is required. Set PRIVATE_APP_ACCESS_TOKEN in your environment variables and retry.');
        }
    }
    /**
     * Make a request to the HubSpot API
     * @param path The API endpoint path
     * @param options Request options including method, body, query params, and headers
     * @returns Promise with the API response
     */
    async request(path, options = {}) {
        const { method = 'GET', body, params } = options;
        // Build URL with query parameters
        let url = `${this.baseUrl}${path}`;
        if (params && Object.keys(params).length > 0) {
            const searchParams = new URLSearchParams();
            Object.entries(params).forEach(([key, value]) => {
                searchParams.append(key, String(value));
            });
            url = `${url}?${searchParams.toString()}`;
        }
        // Prepare request headers
        const requestHeaders = {
            Authorization: `Bearer ${this.accessToken}`,
            'Content-Type': 'application/json',
            'User-Agent': `${APP_NAME}/${APP_VERSION}`,
        };
        // Prepare request options
        const requestOptions = {
            method,
            headers: requestHeaders,
        };
        // Add body for non-GET requests
        if (body && method !== 'GET') {
            requestOptions.body = JSON.stringify(body);
        }
        try {
            const response = await fetch(url, requestOptions);
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(`HubSpot API Error: ${response.status} ${response.statusText} - ${JSON.stringify(errorData)}`);
            }
            // Check if response is empty
            const contentType = response.headers.get('content-type');
            if (contentType && contentType.includes('application/json')) {
                return (await response.json());
            }
            return (await response.text());
        }
        catch (error) {
            if (error instanceof Error) {
                throw error;
            }
            throw new Error(`Failed to make request to HubSpot API: ${String(error)}`);
        }
    }
    // Convenience methods for different HTTP methods
    async get(path, options = {}) {
        return this.request(path, { ...options, method: 'GET' });
    }
    async post(path, options = {}) {
        return this.request(path, { ...options, method: 'POST' });
    }
    async put(path, options = {}) {
        return this.request(path, { ...options, method: 'PUT' });
    }
    async delete(path, options = {}) {
        return this.request(path, { ...options, method: 'DELETE' });
    }
    async patch(path, options = {}) {
        return this.request(path, { ...options, method: 'PATCH' });
    }
}
export default HubSpotClient;
