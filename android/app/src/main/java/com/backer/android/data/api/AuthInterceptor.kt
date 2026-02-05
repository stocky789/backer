package com.backer.android.data.api

import com.backer.android.data.repository.CredentialRepository
import okhttp3.Credentials
import okhttp3.Interceptor
import okhttp3.Response
import javax.inject.Inject
import javax.inject.Singleton

/**
 * OkHttp interceptor that adds Basic Auth credentials to requests.
 */
@Singleton
class AuthInterceptor @Inject constructor(
    private val credentialRepository: CredentialRepository
) : Interceptor {

    override fun intercept(chain: Interceptor.Chain): Response {
        val originalRequest = chain.request()

        // Don't add auth to registration or health check endpoints
        val path = originalRequest.url.encodedPath
        if (path.endsWith("/register") || path.endsWith("/health")) {
            return chain.proceed(originalRequest)
        }

        // Get credentials
        val credentials = credentialRepository.getCredentials()
        if (credentials == null) {
            // No credentials, proceed without auth
            return chain.proceed(originalRequest)
        }

        // Add Basic Auth header
        val authenticatedRequest = originalRequest.newBuilder()
            .header(
                "Authorization",
                Credentials.basic(credentials.clientId, credentials.clientSecret)
            )
            .build()

        return chain.proceed(authenticatedRequest)
    }
}